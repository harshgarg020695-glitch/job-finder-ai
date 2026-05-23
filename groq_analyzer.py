"""
groq_analyzer.py — context scoring via Groq LLM.

Model strategy:
  Primary  : llama-3.1-8b-instant   (14,400 req/day free — high volume)
  Premium  : llama-3.3-70b-versatile (1,000 req/day  — used for top-20 rescore only)
  Fallback : llama-3.2-1b-preview   (emergency)

Rate-limit handling:
  - 2 s minimum between API calls (stays under 30 RPM hard limit)
  - On first 429: set groq_available=False, degrade remaining jobs to keyword-only
  - check_quota(): test call + header read (best-effort)
"""

import os
import json
import time
from typing import Optional
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

_PRIMARY_MODEL = "llama-3.1-8b-instant"
_PREMIUM_MODEL = "llama-3.3-70b-versatile"
_FALLBACK_MODEL = "llama-3.2-1b-preview"

_MIN_CALL_INTERVAL = 2.0  # seconds between calls — keeps RPM ≤ 30

_SKILLS_SIGNALS = [
    'requirements', 'qualifications', 'skills', 'experience required',
    'you will need', 'must have', 'preferred', 'responsibilities',
    'what you bring', 'who you are', 'about you',
]


def assess_description_quality(description: str) -> dict:
    """
    Inspect a job description and return quality metadata.
    Called before scoring so the scorer knows how much to trust its own output.
    """
    if not description or not str(description).strip():
        return {
            "description_quality":      "missing",
            "description_word_count":   0,
            "description_has_requirements": False,
            "description_quality_note": "No description available — score unreliable",
        }

    word_count = len(description.split())
    desc_lower = description.lower()
    has_requirements = any(sig in desc_lower for sig in _SKILLS_SIGNALS)

    if word_count < 50:
        quality = "too_short"
        note    = f"Description only {word_count} words — score may be unreliable"
    elif not has_requirements:
        quality = "no_requirements_listed"
        note    = "No skills/qualifications mentioned — score based on title/context only"
    elif word_count < 150:
        quality = "sparse"
        note    = f"Brief description ({word_count} words) — partial information only"
    else:
        quality = "complete"
        note    = ""

    return {
        "description_quality":          quality,
        "description_word_count":       word_count,
        "description_has_requirements": has_requirements,
        "description_quality_note":     note,
    }


class GroqAnalyzer:
    def __init__(self):
        from job_automation.groq_key_manager import GroqKeyManager
        self.key_manager = GroqKeyManager()

        self.primary_model  = _PRIMARY_MODEL
        self.premium_model  = _PREMIUM_MODEL
        self._model_chain   = [_PRIMARY_MODEL, _PREMIUM_MODEL, _FALLBACK_MODEL]

        # State tracked across a scoring session
        self.groq_available      = True
        self.ai_scored_count     = 0
        self.keyword_only_count  = 0
        self._last_call_time     = 0.0

        # Load scoring config — updated automatically via telemetry.check_for_prompt_updates()
        config_path = "scoring_config.json"
        if os.path.exists(config_path):
            try:
                with open(config_path) as _f:
                    self.scoring_config = json.load(_f)
                print(f"[GroqAnalyzer] Loaded scoring_config.json "
                      f"v{self.scoring_config.get('version', '?')}")
            except Exception as _ce:
                print(f"[GroqAnalyzer] scoring_config.json load failed: {_ce}")
                self.scoring_config = {}
        else:
            self.scoring_config = {}

        print(f"[GroqAnalyzer] Initialized — primary: {self.primary_model} "
              f"| {len(self.key_manager.keys)} key(s)")

    # ── Client factory (key-rotation aware) ───────────────────────────────

    def _get_client(self) -> tuple:
        """
        Return (Groq client, api_key_string) for the best available key.
        Raises RuntimeError (caught by callers) when all keys are exhausted.
        """
        key    = self.key_manager.get_best_key()
        client = Groq(api_key=key)
        return client, key

    # ── Rate-limit helpers ─────────────────────────────────────────────────

    def _wait_for_rate_limit(self):
        """Sleep just enough to keep ≥ 2 s between API calls."""
        elapsed = time.time() - self._last_call_time
        if elapsed < _MIN_CALL_INTERVAL:
            time.sleep(_MIN_CALL_INTERVAL - elapsed)
        self._last_call_time = time.time()

    def _keyword_only_result(self) -> dict:
        self.keyword_only_count += 1
        return {
            "domain_match":    False,
            "transferability": "Unknown",
            "context_score":   50,
            "raw_response":    "keyword-only fallback",
            "model_used":      "keyword-only",
            "scored_by":       "keyword_only",
        }

    def _safe_score(self, raw_value) -> float:
        """
        Robustly convert a raw LLM score value to a 0-100 float.

        Handles common edge cases:
          - int / float already on the right scale  → clamp to [0, 100]
          - float in 0-1 range (e.g. 0.72)          → multiply by 100
          - string like "72", "72%", "72/100"        → extract first number
          - string "0.72"                             → detected as 0-1, scaled
          - None / unparseable                        → default 50.0
        """
        import re
        if isinstance(raw_value, (int, float)):
            val = float(raw_value)
            if val <= 1.0 and val >= 0.0:
                val *= 100          # 0-1 scale → 0-100
            return float(max(0.0, min(100.0, val)))
        if isinstance(raw_value, str):
            nums = re.findall(r'\d+(?:\.\d+)?', str(raw_value))
            if nums:
                val = float(nums[0])
                if val <= 1.0:      # "0.72" → treat as 0-1 scale
                    val *= 100
                return float(max(0.0, min(100.0, val)))
        return 50.0   # safe default — middle score

    # ── Public methods ─────────────────────────────────────────────────────

    def check_quota(self) -> dict:
        """
        Make a minimal 1-token call and read rate-limit headers (best-effort).
        Returns dict with key_status, remaining_requests, reset_time, model.
        """
        try:
            self._wait_for_rate_limit()
            client, key = self._get_client()
            response = client.chat.completions.create(
                model=self.primary_model,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1,
            )
            raw     = getattr(response, "_raw_response", None)
            headers = dict(raw.headers) if raw else {}
            self.key_manager.update_from_headers(key, headers)

            remaining_req = int(headers.get("x-ratelimit-remaining-requests", -1))
            remaining_tok = int(headers.get("x-ratelimit-remaining-tokens", -1))
            reset_req     = headers.get("x-ratelimit-reset-requests", "unknown")
            return {
                "status":             "ok",
                "model":              self.primary_model,
                "active_key":         f"…{key[-6:]}",
                "remaining_requests": remaining_req,
                "remaining_tokens":   remaining_tok,
                "reset_requests":     reset_req,
                "key_status":         self.key_manager.get_status(),
            }
        except RuntimeError as e:
            # All keys exhausted
            return {"status": "exhausted", "message": str(e),
                    "key_status": self.key_manager.get_status()}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── Candidate profile extraction ──────────────────────────────────────

    def extract_candidate_profile(self, resume_text: str) -> dict:
        """
        Extract a structured matching profile from ANY resume.
        Called ONCE per search run, before batch scoring begins.
        Result is cached on the caller and passed into all score_batch() calls.

        Returns a profile dict describing what a strong/weak match looks like
        for THIS specific candidate based on THEIR actual resume content.
        """
        prompt = f"""Analyze this resume and extract a structured job-matching profile.
Be specific to THIS person's actual experience — do not generalize.

Resume:
{resume_text[:3000]}

Return ONLY valid JSON, no other text:
{{
    "title": "their primary job title or target role in 5 words max",
    "level": "junior|mid|senior|executive",
    "years_experience": 0,
    "primary_domain": "MUST be exactly one of these keys: tech_management | developer | data_ml | finance | sales | healthcare | legal | hr | other. Pick the closest match to their background.",
    "core_skills": ["skill1", "skill2", "skill3"],
    "strong_match_if_jd_contains": [
        "3-5 specific phrases that indicate a strong role match",
        "be specific to their domain not generic"
    ],
    "weak_match_if_jd_contains": [
        "3-5 phrases indicating wrong domain for this person",
        "derive these from what their resume does NOT mention"
    ],
    "target_company_types": [
        "types of companies that would value this person"
    ],
    "avoid_if_title_contains": [
        "words in job titles that indicate wrong function for this person"
    ]
}}"""

        try:
            self._wait_for_rate_limit()
            client, key = self._get_client()
            response = client.chat.completions.create(
                model=self.primary_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=600,
                temperature=0.1,
            )
            raw_resp = getattr(response, "_raw_response", None)
            if raw_resp:
                self.key_manager.update_from_headers(key, raw_resp.headers)

            raw = response.choices[0].message.content.strip()
            # Strip markdown fences (model sometimes adds preamble before the block)
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0]
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0]
            # Strip any prose preamble before the first {
            brace = raw.find("{")
            if brace > 0:
                raw = raw[brace:]
            raw = raw.strip()
            profile = json.loads(raw)

            # ── Normalize primary_domain to a known config key ─────────────
            _VALID_DOMAINS = {
                "tech_management", "developer", "data_ml", "finance",
                "sales", "healthcare", "legal", "hr", "other",
            }
            _DOMAIN_KEYWORDS = {
                "tech_management": [
                    "implementation", "delivery", "program", "transformation",
                    "consulting", "ai manager", "project manager", "scrum",
                    "agile", "technology manager", "digital",
                ],
                "developer": [
                    "software", "engineer", "developer", "coding",
                    "backend", "frontend", "full stack", "devops", "swe",
                ],
                "data_ml": [
                    "data scientist", "machine learning", "analytics",
                    "deep learning", "nlp", "data engineer", "ml engineer",
                ],
                "finance": [
                    "investment", "banking", "equity", "portfolio",
                    "valuation", "credit", "trading", "cfa", "corporate finance",
                ],
                "sales": [
                    "sales", "revenue", "account executive", "bd",
                    "business development",
                ],
                "healthcare": [
                    "clinical", "hospital", "ehr", "medical",
                    "pharma", "healthcare it",
                ],
            }

            raw_domain = profile.get("primary_domain", "other").lower().strip()
            if raw_domain in _VALID_DOMAINS:
                profile["primary_domain"] = raw_domain
            else:
                matched = "other"
                for key, keywords in _DOMAIN_KEYWORDS.items():
                    if any(kw in raw_domain for kw in keywords):
                        matched = key
                        break
                profile["primary_domain"] = matched
                if matched != "other":
                    print(f"[Profile] Domain normalized: "
                          f"'{raw_domain[:50]}' → '{matched}'")

            print(
                f"[Profile] Extracted: {profile.get('title')} | "
                f"{profile.get('level')} | domain_key={profile.get('primary_domain')}"
            )
            return profile

        except Exception as e:
            print(f"[Profile] Extraction failed: {e} — using resume text directly")
            return {"raw_resume": resume_text[:1500]}

    def _truncate_description(self, job: dict) -> str:
        """
        Source-aware description truncation.
        LinkedIn/Indeed return full JDs (1 000–2 500 words) — use up to 2 500 chars.
        Google Jobs India / Naukri return snippets — 400 chars is the realistic ceiling.
        Everything else gets 1 000 chars as a safe middle ground.
        """
        desc   = (job.get("description") or "").replace("\n", " ").strip()
        source = (job.get("source") or "").lower()
        if source in ("linkedin", "indeed"):
            return desc[:2500]
        elif source in ("google_jobs_india", "naukri"):
            return desc[:400]
        else:
            return desc[:1000]

    def score_batch(
        self,
        jobs: list[dict],
        resume_text: str,
        candidate_profile: Optional[dict] = None,
    ) -> list[dict]:
        """
        Score up to 5 jobs in a single Groq call, with automatic key rotation.

        candidate_profile: result of extract_candidate_profile().
            When provided the prompt is built from the structured profile
            (stronger, more specific scoring guidance).
            Falls back to raw resume summary when None.

        On a 429 the exhausted key is marked and the next available key is
        tried immediately (up to len(keys) attempts total).  Falls back to
        keyword_only when all keys are exhausted or JSON parsing fails.
        """
        if not jobs:
            return jobs

        if not self.groq_available:
            return self._batch_keyword_fallback(jobs)

        # ── Build profile section ──────────────────────────────────────────
        if candidate_profile and "title" in candidate_profile:
            # Check scoring_config.domain_configs for curated signals first
            domain_config   = self.scoring_config.get("domain_configs", {})
            profile_domain  = candidate_profile.get("primary_domain", "unknown")
            config_source   = "profile"

            if profile_domain in domain_config:
                strong_signals = domain_config[profile_domain].get("strong_signals", [])
                weak_signals   = domain_config[profile_domain].get("weak_signals", [])
                config_source  = "scoring_config"
            else:
                strong_signals = candidate_profile.get("strong_match_if_jd_contains", [])
                weak_signals   = candidate_profile.get("weak_match_if_jd_contains", [])

            avoid = ", ".join(candidate_profile.get("avoid_if_title_contains", []))
            strong = "\n".join(f"  ✓ {s}" for s in strong_signals)
            weak   = "\n".join(f"  ✗ {s}" for s in weak_signals)

            profile_section = (
                f"CANDIDATE PROFILE (extracted from resume):\n"
                f"Title: {candidate_profile.get('title', 'Unknown')}\n"
                f"Level: {candidate_profile.get('level', 'Unknown')} | "
                f"{candidate_profile.get('years_experience', '?')} years experience\n"
                f"Domain: {candidate_profile.get('primary_domain', 'Unknown')}\n"
                f"Core skills: {', '.join(candidate_profile.get('core_skills', [])[:6])}\n"
                f"Strong-match signals (from profile): {', '.join(strong_signals[:5]) if strong_signals else 'see resume'}\n"
                f"Weak-match signals (from profile): {', '.join(weak_signals[:3]) if weak_signals else 'none'}\n"
                f"Avoid if title contains: {avoid if avoid else 'none'}"
            )
            print(
                f"[Score Batch] Using profile: {candidate_profile.get('title','?')} | "
                f"signals from: {config_source} | "
                f"Strong: {strong_signals[:2]}"
            )
        else:
            profile_section = (
                f"CANDIDATE PROFILE (from resume):\n{resume_text[:1200]}"
            )

        # ── Build job blocks ───────────────────────────────────────────────
        job_blocks: list[str] = []
        for i, job in enumerate(jobs, 1):
            desc = self._truncate_description(job)
            job_blocks.append(
                f"[JOB {i}]\n"
                f"Title: {job.get('title', '')}\n"
                f"Company: {job.get('company', '')}\n"
                f"Description: {desc}"
            )

        prompt = (
            "You are a precise job-fit evaluator.\n"
            "Score each job for this candidate. Read carefully.\n\n"
            f"{profile_section}\n\n"
            "SCORING RULES — READ CAREFULLY:\n"
            "- Score based on evidence you can SEE in the job description text\n"
            "- Do NOT score 75+ unless the JD text explicitly states requirements "
            "that this candidate can demonstrably meet\n"
            "- Do NOT score 75+ based on job title alone or keyword presence\n"
            "- For each job, ask: 'Does the description show what this role actually "
            "does day-to-day, and does it match the candidate's background?'\n"
            "- If the description is short (<100 words) and contains no requirements, "
            "score maximum 65 and add \"short_description_unreliable\" to risk_flags\n"
            "- If requirements are listed but the candidate clearly lacks them "
            "(e.g. '10 years SAP' when candidate has no SAP), score ≤50\n"
            "- Score 75+ only when: domain matches, level matches, AND visible "
            "requirements align with the candidate's stated experience\n"
            "- risk_flags must be a JSON array; use [\"clear\"] when no issues\n\n"
            f"JOBS TO SCORE:\n" + "\n".join(job_blocks) + "\n\n"
            f"Return ONLY a JSON array with exactly {len(jobs)} objects, no other text:\n"
            "[\n"
            '  {"job_index": 1, "score": 72, '
            '"score_reason": "specific one-sentence reason citing JD text", '
            '"domain_match": "strong|moderate|weak|unknown", '
            '"risk_flags": ["clear"]},\n'
            "  ...\n"
            "]\n"
        )

        # ── Key-rotation retry loop ─────────────────────────────────────────
        max_attempts = max(len(self.key_manager.keys), 1)
        for attempt in range(max_attempts):
            self._wait_for_rate_limit()
            try:
                client, key = self._get_client()
            except RuntimeError as e:
                # All keys exhausted
                print(f"[Groq Batch] {e}")
                self.groq_available = False
                return self._batch_keyword_fallback(jobs)

            try:
                response = client.chat.completions.create(
                    model=self.primary_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=640,
                    temperature=0.1,
                )

                # Update quota tracking from response headers
                raw_resp = getattr(response, "_raw_response", None)
                if raw_resp:
                    self.key_manager.update_from_headers(key, raw_resp.headers)

                raw = response.choices[0].message.content.strip()
                if "```json" in raw:
                    raw = raw.split("```json")[1].split("```")[0]
                elif "```" in raw:
                    raw = raw.split("```")[1].split("```")[0]

                results = json.loads(raw.strip())
                if not isinstance(results, list):
                    raise ValueError("Response is not a JSON array")

                # Apply scores to jobs
                for result in results:
                    idx = int(result.get("job_index", 0)) - 1
                    if 0 <= idx < len(jobs):
                        raw_score = result.get("score", 0)
                        score = self._safe_score(raw_score)
                        jobs[idx]["score"]              = score
                        jobs[idx]["score_reason"]       = result.get("score_reason", "")
                        jobs[idx]["domain_match"]       = result.get("domain_match", "unknown")
                        jobs[idx]["scored_by"]          = "groq_ai"
                        jobs[idx]["risk_flags"]         = result.get("risk_flags", ["clear"])
                        jobs[idx].setdefault("role_mismatch_reason", "")

                self.ai_scored_count += len(jobs)
                return jobs

            except json.JSONDecodeError as e:
                print(f"[Groq Batch] JSON parse error (attempt {attempt+1}): {e}")
                # Don't rotate key for JSON errors — it's a model response issue
                break

            except Exception as e:
                exc_str = str(e)
                if "429" in exc_str or "rate_limit" in exc_str.lower():
                    self.key_manager.mark_error(key, exc_str)
                    print(f"[Groq Batch] Retrying with next key "
                          f"(attempt {attempt+1}/{max_attempts})…")
                    continue   # try next key
                else:
                    print(f"[Groq Batch] Unexpected error: {exc_str[:120]}")
                    break

        # All attempts exhausted → keyword fallback
        return self._batch_keyword_fallback(jobs)

    def _batch_keyword_fallback(self, jobs: list[dict]) -> list[dict]:
        """Mark all jobs in a batch as keyword_only with safe defaults."""
        for job in jobs:
            job.setdefault("score", 0)
            job.setdefault("scored_by", "keyword_only")
            job.setdefault("role_mismatch_reason", "")
        self.keyword_only_count += len(jobs)
        return jobs

    def analyze_job_match(self, resume: str, job_description: str,
                          max_retries: int = 2, model: Optional[str] = None) -> dict:
        """
        Score a resume ↔ job-description pair for domain fit.
        Used by HybridScorer (premium rescore path).

        On a 429, rotates to the next available Groq key before giving up.
        """
        if not self.groq_available:
            return self._keyword_only_result()

        self._wait_for_rate_limit()

        models_to_try = [model] if model else self._model_chain

        for model_name in models_to_try:
            # Allow one key-rotation retry per model
            max_key_attempts = max(len(self.key_manager.keys), 1)
            for _key_attempt in range(max_key_attempts):
                try:
                    print(f"[GroqAnalyzer] Trying model: {model_name}")
                    client, key = self._get_client()
                    result = self._call_model(
                        resume, job_description, model_name, max_retries, client
                    )
                    if result:
                        result["model_used"] = model_name
                        result["scored_by"]  = "groq_ai"
                        self.ai_scored_count += 1
                        return result
                    break   # got a response (even if empty) — don't retry key

                except RuntimeError:
                    # All keys exhausted
                    self.groq_available = False
                    return self._keyword_only_result()

                except Exception as exc:
                    exc_str = str(exc)
                    if "429" in exc_str or "rate_limit" in exc_str.lower():
                        self.key_manager.mark_error(key, exc_str)
                        print(f"[GroqAnalyzer] Rotating key after 429 on {model_name}…")
                        self._wait_for_rate_limit()
                        continue   # retry same model with next key
                    else:
                        print(f"[GroqAnalyzer] {model_name} failed: {exc_str[:120]}, "
                              "trying next model…")
                        break   # non-rate-limit error — move to next model

        print("[GroqAnalyzer] All models failed — falling back to keyword-only")
        return self._keyword_only_result()

    def scoring_summary(self) -> str:
        total = self.ai_scored_count + self.keyword_only_count
        return (
            f"AI scored: {self.ai_scored_count} jobs | "
            f"Keyword only: {self.keyword_only_count} jobs"
            + (" (Groq limit reached)" if not self.groq_available else "")
            + f" | Total: {total}"
        )

    # ── Internal ───────────────────────────────────────────────────────────

    def _call_model(self, resume: str, job_description: str,
                    model_name: str, max_retries: int,
                    client=None) -> Optional[dict]:
        """client is injected by analyze_job_match for key-rotation support."""
        prompt = f"""Analyze this job match for a career switcher.

RESUME:
{resume[:2000]}

JOB DESCRIPTION:
{job_description[:2000]}

Analyze:
1. What domain is the job in? (Tech/Software/SaaS, Finance/Banking, Healthcare, Real Estate, etc.)
2. What domain is the candidate's PRIMARY experience in?
3. Do they match? (true/false)
4. Transferability: High (80%+ skills transfer), Medium (50-80%), or Low (<50%)
5. Context score: 0-100 based on domain match and transferability

Respond ONLY with valid JSON:
{{
  "job_domain": "Tech/Software/SaaS",
  "candidate_domain": "Real Estate",
  "domain_match": false,
  "transferability": "Medium",
  "context_score": 60,
  "reasoning": "Candidate has PM skills but different domain"
}}"""

        if client is None:
            client, _ = self._get_client()

        for attempt in range(max_retries):
            try:
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=500,
                )
                raw = response.choices[0].message.content

                # Strip markdown fences if present
                if "```json" in raw:
                    raw = raw.split("```json")[1].split("```")[0]
                elif "```" in raw:
                    raw = raw.split("```")[1].split("```")[0]

                parsed = json.loads(raw.strip())
                required = ("domain_match", "transferability", "context_score")
                if all(k in parsed for k in required):
                    parsed["raw_response"] = raw
                    return parsed

                print(f"[GroqAnalyzer] Missing fields in response, retrying…")

            except json.JSONDecodeError as e:
                print(f"[GroqAnalyzer] JSON parse error (attempt {attempt+1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return None

            except Exception:
                raise  # Let caller handle (rate-limit detection)

        return None


if __name__ == "__main__":
    analyzer = GroqAnalyzer()
    print("Quota check:", analyzer.check_quota())

    result = analyzer.analyze_job_match(
        "AI Implementation Manager with 7+ years delivery, Python, LangChain, consulting",
        "Senior AI Implementation Manager — AI/ML delivery, stakeholder management, Python",
    )
    print(f"Model: {result.get('model_used')} | Score: {result.get('context_score')} "
          f"| Domain match: {result.get('domain_match')}")
