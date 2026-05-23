"""
JobAggregator — runs all scrapers, deduplicates, and returns a unified list.

Each scraper is called independently; if one throws, the others still run.
Deduplication key: lowercase(title) + "|" + lowercase(company).

Public methods:
    search(keyword, location, ...)       — single-keyword search across platforms
    deep_search(keyword, location, ...)  — progressive 3-window thorough search
"""

from __future__ import annotations
import hashlib
import re
import sys
import os
from typing import Optional

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _dedup_key(job: dict) -> str:
    raw = job.get("title", "").lower().strip() + "|" + job.get("company", "").lower().strip()
    return hashlib.md5(raw.encode()).hexdigest()


def _freshness_label(posted_date_str: str) -> str:
    """Map posted date to a human-readable label (display only — not used in ranking)."""
    from datetime import datetime
    _LABELS = {
        0: "Posted today",      1: "Posted yesterday",
        2: "2 days ago",        3: "3 days ago",
        4: "4 days ago",        5: "5 days ago",
        6: "6 days ago",        7: "7 days ago",
    }
    if not posted_date_str or str(posted_date_str).strip() in ("None", "", "nan"):
        return "Date unknown"
    try:
        posted   = datetime.strptime(str(posted_date_str)[:10], "%Y-%m-%d")
        days_old = max(0, (datetime.now() - posted).days)
        return _LABELS.get(days_old, f"{days_old} days ago")
    except Exception:
        return "Date unknown"


def location_filter(jobs: list, preferred_city: str) -> list:
    """
    Keep only jobs relevant to the user's preferred city.

    KEEP  : preferred_city match → On-site (no label change)
    KEEP  : remote / hybrid / WFH / pan-india → labelled as location_type
    KEEP  : empty / "india" / "in" suffix → Pan-India
    DROP  : jobs exclusively in another known city
    """
    if not preferred_city:
        return jobs

    city_lower = preferred_city.lower()
    kept: list  = []
    dropped      = 0

    REMOTE_TERMS  = ["remote", "wfh", "work from home", "work-from-home"]
    HYBRID_TERMS  = ["hybrid"]
    FLEXIBLE_TERMS = ["anywhere", "pan india", "pan-india"]

    from collections import Counter
    loc_type_counts: Counter = Counter()

    for job in jobs:
        loc = (job.get("location") or "").strip().lower()

        if city_lower in loc:
            # On-site in the preferred city — no location_type label needed
            kept.append(job)
            loc_type_counts["On-site Hyderabad"] += 1

        elif any(t in loc for t in REMOTE_TERMS):
            job["location_type"] = "Remote (open to Hyderabad)"
            kept.append(job)
            loc_type_counts["Remote (open to Hyderabad)"] += 1

        elif any(t in loc for t in HYBRID_TERMS):
            job["location_type"] = "Hybrid (open to Hyderabad)"
            kept.append(job)
            loc_type_counts["Hybrid (open to Hyderabad)"] += 1

        elif any(t in loc for t in FLEXIBLE_TERMS):
            job["location_type"] = "Flexible location"
            kept.append(job)
            loc_type_counts["Flexible location"] += 1

        elif not loc or loc in ("india", "in", "india ") or loc.startswith("india") \
                or loc.endswith(", in") or loc.endswith(",in"):
            job["location_type"] = "Pan-India"
            kept.append(job)
            loc_type_counts["Pan-India"] += 1

        else:
            dropped += 1

    print(f"[Location Filter] Kept {len(kept)} | Dropped {dropped} (other cities only)")
    for lt, cnt in sorted(loc_type_counts.items(), key=lambda x: -x[1]):
        print(f"  {lt}: {cnt}")
    return kept


def _expand_keyword(keyword: str) -> list[str]:
    """
    Static keyword expansion — used as fallback when no resume is available
    (e.g. healthcheck, /api/search without resume).
    """
    kw = keyword.strip()
    kw_lower = kw.lower()
    variants: list[str] = [kw]

    seniority_prefixes = ("senior", "lead", "head", "director", "vp ", "vice president")
    if not any(kw_lower.startswith(p) for p in seniority_prefixes):
        variants.append(f"Senior {kw}")

    if "manager" in kw_lower:
        lead_variant = re.sub(r'\bmanager\b', 'Lead', kw, flags=re.IGNORECASE).strip()
        variants.append(lead_variant)
    else:
        if not kw_lower.endswith("lead"):
            variants.append(f"{kw} Lead")

    domain_terms = {"ai", "ml", "digital", "tech", "cloud", "data", "product"}
    words = kw.split()
    if len(words) > 2 and words[0].lower() in domain_terms:
        variants.append(" ".join(words[1:]))

    ai_terms = ("ai", "artificial intelligence", "machine learning", "ml ", "llm", "generative")
    if not any(t in kw_lower for t in ai_terms):
        variants.append(f"AI {kw}")

    seen: set[str] = set()
    unique: list[str] = []
    for v in variants:
        key = v.lower()
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return unique[:5]


def expand_keywords(keyword: str, resume_text: str = "") -> list[str]:
    """
    Generate up to 4 keyword variants calibrated to the user's career level.
    Falls back to _expand_keyword() when resume_text is not available.

    Level → strategy:
      senior : add "Senior …", replace Manager→Lead
      mid    : add "Senior …", replace Manager→Consultant
      junior : replace Manager→Analyst, prepend "Associate …"
    Always appends domain-stripped fallback (removes first word if ≥3 words).
    """
    if not resume_text:
        return _expand_keyword(keyword)

    try:
        from hybrid_scorer import detect_profile_type
        profile = detect_profile_type(resume_text)
    except Exception:
        return _expand_keyword(keyword)

    kw       = keyword.strip()
    kw_lower = kw.lower()
    variants: list[str] = [kw]

    if profile['level'] == 'senior':
        if not kw_lower.startswith('senior'):
            variants.append(f"Senior {kw}")
        alt = re.sub(r'\bmanager\b', 'Lead', kw, flags=re.IGNORECASE).strip()
        if alt.lower() != kw_lower:
            variants.append(alt)

    elif profile['level'] == 'mid':
        if not kw_lower.startswith('senior'):
            variants.append(f"Senior {kw}")
        alt = re.sub(r'\bmanager\b', 'Consultant', kw, flags=re.IGNORECASE).strip()
        if alt.lower() != kw_lower:
            variants.append(alt)

    else:  # junior
        alt = re.sub(r'\bmanager\b', 'Analyst', kw, flags=re.IGNORECASE).strip()
        if alt.lower() != kw_lower:
            variants.append(alt)
        if not kw_lower.startswith('associate'):
            variants.append(f"Associate {kw}")

    # Domain-stripped fallback (e.g. "AI Implementation Manager" → "Implementation Manager")
    words = kw.split()
    if len(words) > 2:
        stripped = " ".join(words[1:])
        if stripped.lower() not in {v.lower() for v in variants}:
            variants.append(stripped)

    # Deduplicate, preserve order, cap at 4
    seen:  set[str]  = set()
    final: list[str] = []
    for v in variants:
        v_clean = v.strip()
        if v_clean and v_clean.lower() not in seen:
            seen.add(v_clean.lower())
            final.append(v_clean)
        if len(final) == 4:
            break

    return final


# ── Experience pre-filter ─────────────────────────────────────────────────

def extract_required_experience(description: str) -> int:
    """
    Pull the highest years-of-experience number from a job description.
    Returns 0 when no clear requirement is found.
    """
    if not description:
        return 0
    # Match patterns like "8+ years", "10 years", "5-8 years" (take the higher)
    matches = re.findall(r'(\d+)\s*(?:\+\s*)?years?', description.lower())
    years = [int(m) for m in matches if 1 <= int(m) <= 25]
    return max(years) if years else 0


def experience_filter(jobs: list, candidate_years: int = 8) -> list:
    """
    Drop jobs that explicitly require more than candidate_years + 3.
    Jobs with no detectable experience requirement are always kept.
    """
    ceiling = candidate_years + 3
    kept: list = []
    skipped = 0
    for job in jobs:
        required = extract_required_experience(job.get("description", ""))
        if required > 0 and required > ceiling:
            skipped += 1
        else:
            kept.append(job)
    if skipped:
        print(f"[ExperienceFilter] Removed {skipped} jobs requiring >{ceiling} yrs experience")
    return kept


# ── URL quality filter (FIX 3) ────────────────────────────────────────────

_SPAM_URL_PATTERNS = [
    "liveblog365.com",
    "byethost", "infinityfree.me", "wfh.hstn.me",
    "hirequill.", "hirequorum.", "hireminthub.",
    "freehosting", ".000webhostapp.", "wixsite.com/jobs",
    "jobrapido.com",
    "iceiy.com", "nichesite.org", "likesyou.org", "rf.gd",
    "10001mb.com", "superconceptclasses.in",
    # job-aggregator mirror sites that redirect to category pages
    "careeratlas.online", "hirezilla.", "joborix.", "remotivark.",
    "careerup.", "jobspawn.", "remotetide.", "remotivix.",
    "gigpharix.", "jobvoyage.", "quickswoop.", "hirenixa.",
    "remotepulse.", "wfhforgeon.",
]


def url_quality_filter(jobs: list) -> list:
    """Remove jobs whose URL matches known spam / low-quality aggregator domains."""
    kept    : list = []
    removed : int  = 0
    for job in jobs:
        url = (job.get("url") or "").lower()
        if any(pat in url for pat in _SPAM_URL_PATTERNS):
            removed += 1
            continue
        kept.append(job)
    if removed:
        print(f"[URL Quality Filter] Removed {removed} spam/low-quality domain jobs")
    return kept


# ── Stale-date post-filter (FIX 4) ────────────────────────────────────────

def drop_stale_jobs(jobs: list, max_days: int = 14) -> list:
    """
    Drop jobs whose freshness_label indicates age > max_days.
    Some platforms return stale results despite the hours_old window.
    """
    kept    : list = []
    dropped : int  = 0
    stale_re = re.compile(r"(\d+)\s*days?\s*ago")
    for job in jobs:
        label = (job.get("freshness_label") or "").lower()
        m = stale_re.search(label)
        if m and int(m.group(1)) > max_days:
            dropped += 1
            continue
        kept.append(job)
    if dropped:
        print(f"[Date Filter] Dropped {dropped} jobs older than {max_days} days")
    return kept


# ── Weak-domain score cap ─────────────────────────────────────────────────

def domain_cap(jobs: list) -> list:
    """
    Cap scores on weak-domain jobs to prevent them sailing past the 70% gate.
    Threshold is read from scoring_config.json (default 50 if not available).

    Must be called AFTER Groq scoring and BEFORE _passes_threshold().
    """
    try:
        cap_threshold = json.load(
            open("scoring_config.json")
        ).get("thresholds", {}).get("domain_cap_weak", 50)
    except Exception:
        cap_threshold = 50

    capped = 0
    for job in jobs:
        if job.get("domain_match") == "weak":
            original = float(job.get("score", 0) or 0)
            if original > cap_threshold:
                job["score"] = float(cap_threshold)
                job["score_reason"] = (
                    (job.get("score_reason") or "") +
                    f" [Domain cap: weak domain, capped at {cap_threshold}%]"
                )
                capped += 1
    if capped:
        print(f"[DomainCap] Capped {capped} weak-domain jobs to ≤{cap_threshold}%")
    return jobs


# ── Output field spec (FIX 5) ──────────────────────────────────────────────

MIN_FIT_SCORE                 = 70   # threshold for AI-scored jobs
MIN_FIT_SCORE_INCOMPLETE_DESC = 60   # lower gate for jobs with sparse/missing descriptions
KEYWORD_ONLY_MIN_SCORE        = 50   # gate when Groq unavailable (keyword scoring only)
TARGET_JOBS                   = 50

_CLEAN_FIELDS = [
    "score", "title", "company", "location", "location_type",
    "source", "posted", "freshness_label",
    "url", "description", "description_quality", "description_word_count",
    "description_quality_note", "scored_by", "role_mismatch_reason",
    "domain_match", "risk_flags",
    "salary_min_lpa", "salary_max_lpa", "salary_label", "salary_fit",
]


def _passes_threshold(job: dict, min_fit_score: int) -> bool:
    """
    True when the job's score clears the quality gate.

    - no_description / error / unscored  → always rejected
    - keyword_only                        → must reach KEYWORD_ONLY_MIN_SCORE (50)
    - AI-scored with incomplete desc      → must reach MIN_FIT_SCORE_INCOMPLETE_DESC (60)
    - AI-scored with complete desc        → must reach min_fit_score (default 70)
    """
    scored_by   = job.get("scored_by", "")
    score       = job.get("score", 0)
    desc_quality = job.get("description_quality", "complete")

    if scored_by in ("no_description", "error", ""):
        return False
    if scored_by == "keyword_only":
        return score >= KEYWORD_ONLY_MIN_SCORE

    # AI-scored — apply a lower bar when description was too short / sparse
    if desc_quality in ("no_requirements_listed", "too_short", "missing"):
        threshold = MIN_FIT_SCORE_INCOMPLETE_DESC
    else:
        threshold = min_fit_score

    return score >= threshold


def _clean_job_dict(job: dict) -> dict:
    """Return a job dict with exactly the documented output fields."""
    cleaned = {f: job.get(f, "") for f in _CLEAN_FIELDS}
    if cleaned["score"] == "":
        cleaned["score"] = 0
    desc = str(cleaned.get("description") or "")
    cleaned["description"] = desc[:2000]
    return cleaned


class JobAggregator:

    def __init__(self):
        self._last_run_meta: dict = {}

    # ── Single-keyword search ──────────────────────────────────────────────

    def search(
        self,
        keyword: str,
        location: str = "Hyderabad, India",
        max_results: int = 30,
        max_per_platform: Optional[int] = None,
        platforms: Optional[list[str]] = None,
        hours_old: int = 168,
        skip_seen_filter: bool = False,
    ) -> list[dict]:
        """
        Aggregate raw (unscored) jobs from all requested platforms.

        Returns deduplicated, location-filtered list.
        Scoring and freshness happen in deep_search(), not here.
        """
        if max_per_platform is not None:
            max_results = max_per_platform

        if platforms is None:
            platforms = ["linkedin", "indeed", "naukri"]

        all_jobs: list[dict] = []
        counts: dict[str, int] = {}

        # ── JobSpy / job_scraper (LinkedIn + Indeed) ──────────────────────
        jobspy_platforms = [p for p in platforms if p in ("linkedin", "indeed")]
        if jobspy_platforms:
            try:
                from job_automation.job_scraper import scrape_jobs
                jobs = scrape_jobs(
                    keyword=keyword,
                    location=location,
                    platforms=jobspy_platforms,
                    max_results=max_results,
                    hours_old=hours_old,
                )
                for p in jobspy_platforms:
                    counts[p] = sum(1 for j in jobs if j["source"] == p)
                all_jobs.extend(jobs)
            except Exception as e:
                print(f"[Aggregator] JobSpy scraper failed: {e}")
                for p in jobspy_platforms:
                    counts[p] = 0

        # ── Google Jobs India (Serper/SerpAPI) ────────────────────────────
        if "naukri" in platforms or "google_jobs_india" in platforms:
            key = "google_jobs_india" if "google_jobs_india" in platforms else "naukri"
            try:
                from job_automation.naukri_scraper import NaukriScraper
                jobs = NaukriScraper().search(
                    keyword=keyword,
                    location=location,
                    max_results=max_results,
                    hours_old=hours_old,
                )
                counts[key] = len(jobs)
                all_jobs.extend(jobs)
            except Exception as e:
                print(f"[Aggregator] Google Jobs scraper failed: {e}")
                counts[key] = 0

        # ── Deduplication ──────────────────────────────────────────────────
        seen: set[str] = set()
        unique: list[dict] = []
        for job in all_jobs:
            k = _dedup_key(job)
            if k not in seen:
                seen.add(k)
                unique.append(job)

        # ── Location filter ────────────────────────────────────────────────
        preferred_city = location.split(",")[0].strip()
        unique = location_filter(unique, preferred_city)

        # ── URL quality filter ─────────────────────────────────────────────
        unique = url_quality_filter(unique)

        # ── Summary ────────────────────────────────────────────────────────
        print("\n" + "─" * 50)
        print("AGGREGATOR SUMMARY")
        print("─" * 50)
        for platform in ["linkedin", "indeed", "naukri", "google_jobs_india"]:
            if platform in counts:
                print(f"  {platform:<20}: {counts[platform]:>4} jobs")
        print(f"  {'total raw':<12}: {len(all_jobs):>4} jobs")
        print(f"  {'after dedup':<12}: {len(unique):>4} jobs")
        print("─" * 50 + "\n")

        # ── Cross-run deduplication ────────────────────────────────────────
        if not skip_seen_filter:
            try:
                from job_automation.seen_jobs import filter_seen, mark_seen
                unique, skipped = filter_seen(unique)
                mark_seen(unique)
                print(f"[SeenJobs] {len(unique)} new jobs, {skipped} already seen skipped")
                if unique:
                    unique[0]["_meta_new_jobs"]     = len(unique)
                    unique[0]["_meta_skipped_seen"] = skipped
            except Exception as e:
                print(f"[SeenJobs] Warning: {e}")

        return unique

    # ── Platform fan-out helper ────────────────────────────────────────────

    def _search_one(
        self,
        kw: str,
        loc: str,
        hours_old: int,
        platforms: list[str],
        max_per_platform: int,
    ) -> list[dict]:
        """Single (keyword, location) search — safe to run in a thread."""
        try:
            return self.search(
                keyword=kw,
                location=loc,
                max_per_platform=max_per_platform,
                platforms=platforms,
                hours_old=hours_old,
                skip_seen_filter=True,
            )
        except Exception as e:
            print(f"  [Thread] ERROR {kw!r} / {loc!r}: {e}")
            return []

    def _run_all_platforms(
        self,
        keyword: str,
        location: str,
        hours_old: int,
        platforms: list[str],
        max_per_platform: int,
        keywords: list[str] | None = None,
        start_time: float | None = None,
    ) -> list[dict]:
        """
        Expand keyword × [city, Remote India], call search() for each pair
        in parallel (ThreadPoolExecutor), global-dedup, location-filter.

        `keywords` may be pre-computed by deep_search() using resume context;
        falls back to _expand_keyword() when not provided.
        """
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed

        t0 = start_time if start_time is not None else time.time()

        if keywords is None:
            keywords = _expand_keyword(keyword)
        locations = [location, "Remote India"]
        tasks = [(kw, loc) for kw in keywords for loc in locations]

        elapsed = time.time() - t0
        print(f"\n[{elapsed:.0f}s] Launching {len(tasks)} parallel searches "
              f"({len(keywords)} keywords × {len(locations)} locations) …")
        print(f"  Keywords : {keywords}")
        print(f"  Locations: {locations}")
        print(f"  Window   : {hours_old}h | Platforms: {platforms}")

        # Collect results keyed by task so we can merge in deterministic order
        results: dict[tuple[str, str], list[dict]] = {}
        completed_count = 0

        with ThreadPoolExecutor(max_workers=5) as pool:
            future_map = {
                pool.submit(
                    self._search_one, kw, loc, hours_old, platforms, max_per_platform
                ): (kw, loc)
                for kw, loc in tasks
            }
            for future in as_completed(future_map):
                kw, loc = future_map[future]
                completed_count += 1
                batch = future.result()
                results[(kw, loc)] = batch
                elapsed = time.time() - t0
                print(f"  [{elapsed:.0f}s] ({completed_count}/{len(tasks)}) "
                      f"{kw!r} / {loc!r} → {len(batch)} jobs")

        # Merge in original task order, dedup title+company
        all_jobs: list[dict] = []
        global_seen: set[str] = set()
        for kw, loc in tasks:
            for job in results.get((kw, loc), []):
                k = _dedup_key(job)
                if k not in global_seen:
                    global_seen.add(k)
                    all_jobs.append(job)

        before_loc = len(all_jobs)
        preferred_city = location.split(",")[0].strip()
        all_jobs = location_filter(all_jobs, preferred_city)
        elapsed = time.time() - t0
        dropped = before_loc - len(all_jobs)
        print(f"  [{elapsed:.0f}s] Location filter: {before_loc} → {len(all_jobs)} jobs"
              + (f" (dropped {dropped} outside {preferred_city})" if dropped else ""))
        return all_jobs

    # ── Scoring helpers ────────────────────────────────────────────────────

    def _score_jobs_first_pass(
        self,
        jobs: list[dict],
        resume_text: str,
        existing_scorer=None,
        start_time: float | None = None,
        candidate_profile: Optional[dict] = None,
    ):
        """
        Score jobs with primary 8b model in batches of 5 (one Groq call per batch).
        Attaches description_quality fields.

        candidate_profile: structured profile from extract_candidate_profile().
            When provided, scoring prompt uses specific match signals extracted
            from THIS resume rather than the raw resume text.

        Returns (scored_jobs, scorer) — scorer is passed back so quota state
        is preserved across multiple windows.
        """
        import time
        from hybrid_scorer import HybridScorer
        from groq_analyzer import assess_description_quality

        BATCH_SIZE = 5
        t0     = start_time if start_time is not None else time.time()
        scorer = existing_scorer or HybridScorer()
        groq   = scorer.groq_analyzer

        # ── Attach description quality + mark no-description jobs ────────
        for job in jobs:
            desc = job.get("description", "")
            job.update(assess_description_quality(desc))
            if not desc:
                job["score"]                = 0
                job["scored_by"]            = "no_description"
                job["role_mismatch_reason"] = ""
                job["domain_match"]         = "unknown"

        # ── Batch-score jobs that have descriptions ───────────────────────
        scoreable = [j for j in jobs if j.get("scored_by") != "no_description"]
        total = len(scoreable)
        print(f"  Scoring {total} jobs in batches of {BATCH_SIZE} "
              f"(~{-(-total // BATCH_SIZE)} Groq calls)…")

        for i in range(0, total, BATCH_SIZE):
            batch = scoreable[i:i + BATCH_SIZE]
            groq.score_batch(batch, resume_text, candidate_profile=candidate_profile)
            # Ensure every job in the batch has all required fields
            for job in batch:
                job.setdefault("score", 0)
                job.setdefault("scored_by", "keyword_only")
                job.setdefault("role_mismatch_reason", "")
                job.setdefault("domain_match", "unknown")
            scored_so_far = min(i + BATCH_SIZE, total)
            elapsed = time.time() - t0
            pct = scored_so_far / total * 100 if total else 0
            print(f"  [{elapsed:.0f}s] Scoring… {scored_so_far}/{total} ({pct:.0f}%) "
                  f"| groq_ai: {groq.ai_scored_count} "
                  f"| keyword_only: {groq.keyword_only_count}")

        return jobs, scorer

    def _premium_rescore(
        self, top_jobs: list[dict], resume_text: str, scorer
    ) -> None:
        """Rescore top_jobs in-place with the premium 70b model."""
        groq = scorer.groq_analyzer
        print(f"\nRe-scoring top {len(top_jobs)} with {groq.premium_model}…")
        for job in top_jobs:
            desc = job.get("description", "")
            if not desc:
                continue
            try:
                result = scorer.score_job(resume_text, {
                    "title":       job.get("title", ""),
                    "company":     job.get("company", ""),
                    "description": desc,
                })
                job["score"]                = result["final_score"]
                job["scored_by"]            = "groq_premium"
                job["role_mismatch_reason"] = result.get("role_mismatch_reason", "")
            except Exception as se:
                print(f"  Premium rescore failed for {job.get('title')}: {se}")
        print(f"Premium rescore done. {groq.scoring_summary()}\n")

    # ── Progressive deep search ────────────────────────────────────────────

    def deep_search(
        self,
        keyword: str,
        location: str = "Hyderabad",
        resume_text: str = "",
        min_fit_score: int = MIN_FIT_SCORE,
        target: int = TARGET_JOBS,
        platforms: Optional[list[str]] = None,
        max_per_platform: int = 20,
    ) -> list[dict]:
        """
        Progressive 3-window deep search.

        Tries 24h → 3-day → 7-day windows, stopping as soon as `target`
        jobs scoring ≥ `min_fit_score` are found. New jobs are scored
        incrementally per window; quota state is shared across windows.

        Ranking: fit score DESC only — freshness is display metadata, not ranking.

        Returns list[dict] with exactly the fields in _CLEAN_FIELDS.
        Metadata for callers that need it is stored in self._last_run_meta.
        """
        import time
        start_time = time.time()

        if platforms is None:
            platforms = ["linkedin", "indeed", "naukri"]

        WINDOWS = [
            (24,  "last 24 hours"),
            (72,  "last 3 days"),
            (168, "last 7 days"),
        ]

        # ── Keyword expansion (once, using resume for level-aware variants) ──
        kw_variants = expand_keywords(keyword, resume_text)
        print(f"\n{'='*60}")
        print(f"DEEP SEARCH: {keyword!r} in {location}")
        print(f"Keywords ({len(kw_variants)}): {kw_variants}")
        print(f"Target: {target} jobs with fit ≥ {min_fit_score}% | Platforms: {platforms}")
        print(f"{'='*60}")

        all_scored: dict[str, dict] = {}   # dedup_key → scored job (grows across windows)
        global_raw_seen: set[str]   = set()
        scorer      = None
        window_used = WINDOWS[-1][1]

        # ── Extract candidate profile ONCE before scoring windows ──────────
        candidate_profile: Optional[dict] = None
        if resume_text:
            try:
                from groq_analyzer import GroqAnalyzer
                _profile_groq = GroqAnalyzer()
                candidate_profile = _profile_groq.extract_candidate_profile(resume_text)
                elapsed = time.time() - start_time
                print(f"  [{elapsed:.0f}s] Profile extracted: "
                      f"{candidate_profile.get('title','?')} | "
                      f"{candidate_profile.get('level','?')} | "
                      f"strong signals: "
                      f"{candidate_profile.get('strong_match_if_jd_contains',['?'])[:2]}")
            except Exception as pe:
                print(f"  [Profile] Extraction failed: {pe} — scoring with raw resume text")
                candidate_profile = None

        for hours, label in WINDOWS:
            elapsed = time.time() - start_time
            print(f"\n{'─'*50}")
            print(f"[{elapsed:.0f}s] Window: {label}")
            print(f"{'─'*50}")

            raw_jobs = self._run_all_platforms(
                keyword, location, hours, platforms, max_per_platform,
                keywords=kw_variants,
                start_time=start_time,
            )

            # Only score jobs not already processed in a previous window
            new_jobs = []
            for job in raw_jobs:
                k = _dedup_key(job)
                if k not in global_raw_seen:
                    global_raw_seen.add(k)
                    new_jobs.append(job)

            # ── Pre-scoring filters (saves Groq quota) ─────────────────────
            before_filters = len(new_jobs)
            try:
                from company_filter import company_filter
                new_jobs = company_filter(new_jobs)
            except ImportError:
                pass
            new_jobs = url_quality_filter(new_jobs)
            new_jobs = experience_filter(new_jobs, candidate_years=8)
            try:
                from job_automation.salary_filter import salary_filter
                new_jobs = salary_filter(new_jobs)
            except ImportError:
                pass
            filtered_out = before_filters - len(new_jobs)

            elapsed = time.time() - start_time
            print(f"  [{elapsed:.0f}s] {label}: {len(raw_jobs)} raw total, "
                  f"{len(new_jobs)} new to score"
                  + (f" ({filtered_out} pre-filtered)" if filtered_out else ""))

            if resume_text and new_jobs:
                new_jobs, scorer = self._score_jobs_first_pass(
                    new_jobs, resume_text,
                    existing_scorer=scorer,
                    start_time=start_time,
                    candidate_profile=candidate_profile,
                )

            for job in new_jobs:
                job["freshness_label"] = _freshness_label(job.get("posted", ""))
                all_scored[_dedup_key(job)] = job

            suitable_count = sum(
                1 for j in all_scored.values()
                if _passes_threshold(j, min_fit_score)
            )

            elapsed = time.time() - start_time
            if suitable_count >= target:
                print(f"  [{elapsed:.0f}s] {label}: {suitable_count} suitable jobs ✓ enough — stopping")
            else:
                next_window = "3 days" if hours == 24 else "7 days"
                print(f"  [{elapsed:.0f}s] {label}: {suitable_count} suitable jobs "
                      f"(need {target}) → expanding to {next_window}…")

            window_used = label

            if suitable_count >= target:
                break

        # ── Sort by fit score (freshness is NOT a ranking factor) ──────────
        all_jobs = list(all_scored.values())

        # domain_cap BEFORE threshold gate — prevents weak-domain jobs from
        # passing the 70% gate simply because Groq over-scored them
        if resume_text:
            all_jobs = domain_cap(all_jobs)

        if resume_text:
            all_jobs = [j for j in all_jobs if _passes_threshold(j, min_fit_score)]

        all_jobs.sort(key=lambda j: j.get("score", 0), reverse=True)

        # ── Premium rescore top-20 ─────────────────────────────────────────
        if resume_text and scorer and scorer.groq_analyzer.groq_available and all_jobs:
            self._premium_rescore(all_jobs[:20], resume_text, scorer)
            all_jobs.sort(key=lambda j: j.get("score", 0), reverse=True)
            # Re-apply threshold — premium rescore can lower borderline scores
            all_jobs = [j for j in all_jobs if _passes_threshold(j, min_fit_score)]

        # ── FINAL ROLE PENALTY PASS — catches IC roles Groq scored too high ─
        if resume_text and all_jobs:
            try:
                from hybrid_scorer import role_mismatch_penalty, detect_profile_type
                profile = detect_profile_type(resume_text)
                penalised_count = 0
                for job in all_jobs:
                    # Skip jobs already marked by the pre-Groq filter
                    if "+role_penalty" in (job.get("scored_by") or ""):
                        continue
                    title         = job.get("title", "")
                    current_score = job.get("score", 0)
                    new_score, reason = role_mismatch_penalty(
                        title, resume_text, current_score, profile=profile
                    )
                    if new_score < current_score:
                        job["score"]                = new_score
                        job["role_mismatch_reason"] = reason
                        job["scored_by"]            = (job.get("scored_by") or "") + "+role_penalty"
                        penalised_count += 1
                if penalised_count:
                    print(f"[RolePenalty] Post-scoring pass: penalised {penalised_count} jobs")
                # Re-gate: newly penalised jobs may now fall below threshold
                all_jobs = [j for j in all_jobs if _passes_threshold(j, min_fit_score)]
            except Exception as rp_err:
                print(f"[RolePenalty] Warning: {rp_err}")

        # ── Drop stale jobs (FIX 4) ───────────────────────────────────────
        all_jobs = drop_stale_jobs(all_jobs, max_days=14)

        final = [_clean_job_dict(j) for j in all_jobs[:target]]

        # ── Platform breakdown ─────────────────────────────────────────────
        by_platform: dict[str, int] = {}
        for p in platforms:
            aliases = {p, "google_jobs_india"} if p == "naukri" else {p}
            by_platform[p] = sum(1 for j in final if j.get("source") in aliases)

        # ── Cross-run deduplication ────────────────────────────────────────
        skipped_seen = 0
        try:
            from job_automation.seen_jobs import filter_seen, mark_seen
            final, skipped_seen = filter_seen(final)
            mark_seen(final)
            print(f"[SeenJobs] {len(final)} new jobs, {skipped_seen} already seen skipped")
        except Exception as e:
            print(f"[SeenJobs] Warning: {e}")

        runtime = round(time.time() - start_time, 1)
        ai_count = scorer.groq_analyzer.ai_scored_count if scorer else 0
        kw_count = scorer.groq_analyzer.keyword_only_count if scorer else 0
        print(
            f"\n{'='*60}\n"
            f"COMPLETE in {runtime/60:.1f} min | Jobs: {len(final)} | "
            f"AI scored: {ai_count} | Keyword-only: {kw_count} | "
            f"Window: {window_used}\n"
            f"{'='*60}\n"
        )

        self._last_run_meta = {
            "window_used":        window_used,
            "keywords_searched":  kw_variants,
            "locations_searched": [location, "Remote India"],
            "runtime_seconds":    runtime,
            "scored":             bool(resume_text),
            "new_jobs":           len(final),
            "skipped_seen":       skipped_seen,
            "by_platform":        by_platform,
        }

        # ── Feedback capture (non-critical — never blocks results) ─────────
        try:
            from job_automation.feedback import save_search_run
            fb_run_id = save_search_run(
                keyword=keyword,
                location=location,
                jobs=final,
                profile=candidate_profile or {},
                runtime=runtime,
                window=window_used,
            )
            # Attach run_id so the frontend can link user actions back to this run
            for job in final:
                job["_run_id"] = fb_run_id
            print(f"[Feedback] Saved search run #{fb_run_id} to feedback.db")
        except Exception as _fb_err:
            print(f"[Feedback] Save failed (non-critical): {_fb_err}")

        return final
