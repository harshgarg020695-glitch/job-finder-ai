"""
Job Finder API - Flask backend
Auto-detects career switching via domain analysis.

# ── ENDPOINTS ────────────────────────────────────────────────────────────────
# POST /api/search          — quick search, 1 keyword, ~2-3 min, ~27 jobs
# POST /api/deep-search     — thorough search, auto-expands keywords,
#                             15-20 min, ~300 jobs across 3 platforms
# POST /api/auto-search-jobs — DEPRECATED: use /api/deep-search instead
# POST /api/parse-resume    — extract text from PDF / DOCX upload
# POST /api/suggest-titles  — AI-suggest job titles from resume text
# POST /api/verify-jobs     — check job URL reachability
# GET  /                    — serves index.html
# ─────────────────────────────────────────────────────────────────────────────
"""

import logging
import os
import io
import re
import traceback
from logging.handlers import RotatingFileHandler

from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

from version import CURRENT_VERSION
from serper_job_finder import SerperJobFinder
from job_fetcher import JobFetcher
from hybrid_scorer import HybridScorer
from title_suggester import TitleSuggester
from platform_filter import filter_jobs
from job_verifier import verify_all_jobs

load_dotenv()

# ── SSE progress queues (session_id → queue.Queue) ─────────────────────────
import queue as _queue
_progress_queues: dict[str, _queue.Queue] = {}


def _score_label(score: float) -> str:
    """Return a human-readable fit label for a numeric job score (0–100)."""
    if score >= 90:
        return "Excellent fit"
    elif score >= 75:
        return "Strong fit"
    elif score >= 60:
        return "Good fit"
    elif score >= 40:
        return "Moderate fit"
    else:
        return "Poor fit"

# ── Logging setup ──────────────────────────────────────────────
os.makedirs('logs', exist_ok=True)

_FMT = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s',
                          datefmt='%Y-%m-%d %H:%M:%S')

def _make_logger(name, filename, level=logging.DEBUG):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if not logger.handlers:
        fh = RotatingFileHandler(f'logs/{filename}', maxBytes=10_485_760, backupCount=5)
        fh.setFormatter(_FMT)
        logger.addHandler(fh)
        ch = logging.StreamHandler()
        ch.setFormatter(_FMT)
        ch.setLevel(logging.INFO)
        logger.addHandler(ch)
    return logger

app_logger    = _make_logger('flask_app',       'flask_app.log')
search_logger = _make_logger('job_searches',    'job_searches.log')
filter_logger = _make_logger('platform_filter', 'platform_filter.log')

# ── Flask app ──────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    on_breach=lambda limit: (
        jsonify({
            "error":   "Rate limit exceeded",
            "message": f"This endpoint is limited to {limit.limit}. "
                       "Try again later.",
        }),
        429,
    ),
)

_INDIAN_CITIES = [
    'Hyderabad', 'Bangalore', 'Bengaluru', 'Delhi', 'Mumbai', 'Pune',
    'Chennai', 'Kolkata', 'Ahmedabad', 'Gurgaon', 'Gurugram', 'Noida',
    'NCR', 'Kochi', 'Thiruvananthapuram', 'Indore', 'Bhopal', 'Jaipur',
    'Chandigarh', 'Lucknow', 'Nagpur', 'Surat', 'Vadodara', 'Coimbatore',
]


def extract_location_from_resume(resume_text):
    for section in (resume_text[:500], resume_text):
        for city in _INDIAN_CITIES:
            if re.search(r'\b' + re.escape(city) + r'\b', section, re.IGNORECASE):
                return city
    return None


@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/api/parse-resume', methods=['POST'])
def parse_resume():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files['file']
    filename = file.filename.lower()

    try:
        if filename.endswith('.pdf'):
            import pdfplumber
            with pdfplumber.open(io.BytesIO(file.read())) as pdf:
                text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
        elif filename.endswith('.docx'):
            import docx
            doc = docx.Document(io.BytesIO(file.read()))
            text = '\n'.join(p.text for p in doc.paragraphs if p.text.strip())
        else:
            return jsonify({"error": "Only PDF and DOCX files are supported"}), 400

        text = text.strip()
        if not text:
            return jsonify({"error": "Could not extract text from file"}), 400

        app_logger.info(f"[parse-resume] Extracted {len(text)} chars from {filename}")
        return jsonify({"success": True, "text": text})

    except Exception as e:
        app_logger.error(f"[parse-resume] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/suggest-titles', methods=['POST'])
def suggest_titles():
    try:
        data = request.json
        resume = data.get('resume', '')

        if not resume:
            return jsonify({"error": "Resume is required"}), 400

        suggester = TitleSuggester()
        titles_result = suggester.suggest_titles(resume)

        if isinstance(titles_result, list):
            titles = titles_result
        elif isinstance(titles_result, str):
            try:
                import json
                titles = json.loads(titles_result)
                if not isinstance(titles, list):
                    titles = [titles_result]
            except Exception:
                titles = [t.strip() for t in titles_result.replace('\n', ',').split(',') if t.strip()]
        elif isinstance(titles_result, dict):
            titles = titles_result.get('titles', [])
        else:
            titles = []

        if not titles:
            titles = ["Product Manager", "Senior Product Manager",
                      "Technical Product Manager", "AI Product Manager", "Product Lead"]
            app_logger.warning("TitleSuggester returned no results, using defaults")

        titles = [str(t) for t in titles if t][:10]
        app_logger.info(f"Returning {len(titles)} titles")
        return jsonify({"success": True, "titles": titles})

    except Exception as e:
        app_logger.error(f"suggest_titles error: {e}\n{traceback.format_exc()}")
        return jsonify({"success": True, "titles": [
            "Product Manager", "Senior Product Manager",
            "Technical Product Manager", "AI Product Manager", "Product Lead"
        ]}), 200


@app.route('/api/auto-search-jobs', methods=['POST'])
def auto_search_jobs():
    search_logger.warning(
        "[DEPRECATED] /api/auto-search-jobs — use /api/deep-search instead. "
        "This endpoint still works but will not receive new features."
    )
    search_logger.info("=" * 70)
    search_logger.info("NEW JOB SEARCH (via deprecated auto-search-jobs)")
    search_logger.info("=" * 70)

    try:
        data = request.json
        resume         = data.get('resume', '')
        job_titles     = data.get('job_titles', [])
        locations_in   = data.get('locations', [])
        include_remote = data.get('include_remote', True)
        max_per_query  = data.get('max_results_per_title', 5)

        if not resume:
            return jsonify({"error": "Resume is required"}), 400
        if not job_titles:
            return jsonify({"error": "At least one job title is required"}), 400

        if locations_in:
            locations = [l.strip() for l in locations_in if l.strip()]
        else:
            detected = extract_location_from_resume(resume)
            locations = [detected] if detected else ['India']
            search_logger.info(f"Auto-detected location: {locations}")

        search_logger.info(f"Titles: {job_titles}")
        search_logger.info(f"Locations: {locations}")
        search_logger.info(f"Include remote: {include_remote}")

        finder    = SerperJobFinder()
        raw_jobs  = []
        seen_keys = set()

        def _add(jobs):
            for j in jobs:
                k = f"{j['company']}|{j['title']}"
                if k not in seen_keys:
                    seen_keys.add(k)
                    raw_jobs.append(j)

        for title in job_titles:
            for loc in locations:
                q = f"{title} in {loc}"
                search_logger.info(f"Searching: {q}")
                _add(finder.search_query(q, max_per_query))

        if include_remote:
            for title in job_titles:
                q = f"{title} remote India"
                search_logger.info(f"Searching: {q}")
                _add(finder.search_query(q, max_per_query))

        search_logger.info(f"Raw jobs before filter: {len(raw_jobs)}")

        if not raw_jobs:
            return jsonify({"success": False, "error": "No jobs found", "total_found": 0}), 404

        # filter_jobs with default skip_filter=False — SerpAPI results need vetting
        raw_jobs = filter_jobs(raw_jobs)
        search_logger.info(f"Jobs after platform filter: {len(raw_jobs)}")

        if not raw_jobs:
            return jsonify({"success": False, "error": "No jobs from trusted platforms found", "total_found": 0}), 404

        fetcher        = JobFetcher()
        jobs_with_desc = fetcher.batch_fetch(raw_jobs)

        scorer      = HybridScorer()
        scored_jobs = []

        for job in jobs_with_desc:
            if not job.get('description'):
                continue

            score_result = scorer.score_job(resume, {
                'title':       job['title'],
                'company':     job['company'],
                'description': job['description'],
            })

            _fs = score_result['final_score']
            job['url']             = job.get('apply_link', '#')
            job['final_score']     = _fs
            job['score_label']     = _score_label(_fs)
            job['keyword_score']   = score_result['keyword_score']
            job['context_score']   = score_result['context_score']
            job['recommendation']  = score_result['recommendation']
            job['model_used']      = score_result.get('model_used', 'unknown')
            job['domain_match']    = score_result.get('domain_match', False)
            job['transferability'] = score_result.get('transferability', 'Unknown')
            job['weights_used']    = score_result.get('weights_used', {})

            scored_jobs.append(job)

        scored_jobs.sort(key=lambda x: x['final_score'], reverse=True)
        search_logger.info(f"Scored jobs returned: {len(scored_jobs)}")
        search_logger.info("=" * 70)

        return jsonify({
            "success":           True,
            "locations_searched": locations,
            "include_remote":    include_remote,
            "total_found":       len(raw_jobs),
            "total_scored":      len(scored_jobs),
            "all_jobs":          scored_jobs,
        })

    except Exception as e:
        search_logger.error(f"auto_search_jobs error: {e}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/verify-jobs', methods=['POST'])
def verify_jobs():
    try:
        data = request.json
        jobs = data.get('jobs', [])

        if not jobs:
            return jsonify({"error": "No jobs provided"}), 400

        results   = verify_all_jobs(jobs, max_workers=5)
        reachable = sum(1 for r in results if r['reachable'])
        app_logger.info(f"[verify-jobs] {reachable}/{len(results)} URLs reachable")

        return jsonify({
            "success":     True,
            "total":       len(results),
            "reachable":   reachable,
            "unreachable": len(results) - reachable,
            "results":     results,
        })

    except Exception as e:
        app_logger.error(f"verify_jobs error: {e}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/search', methods=['POST'])
@limiter.limit("10 per hour")
def search_jobs():
    """
    Unified job search across LinkedIn, Indeed, and Naukri.

    Body (JSON):
        keyword               str   required
        location              str   default "India"
        platforms             list  default ["linkedin","indeed","naukri"]
        max_per_platform      int   default 30
        hours_old             int   default 72
        score_against_resume  bool  default false
        resume                str   required when score_against_resume=true

    Response:
        {jobs, total, by_platform, keyword, location, scored}
        Each job includes score + score_reason when scoring is enabled.
    """
    data = request.get_json(silent=True) or {}

    keyword = (data.get('keyword') or '').strip()
    if not keyword:
        return jsonify({"error": "keyword is required"}), 400

    location              = (data.get('location') or 'India').strip()
    platforms             = data.get('platforms') or ['linkedin', 'indeed', 'naukri']
    max_per_platform      = int(data.get('max_per_platform') or 30)
    hours_old             = int(data.get('hours_old') or 168)
    score_against_resume  = bool(data.get('score_against_resume', False))
    resume                = (data.get('resume') or '').strip()

    if score_against_resume and not resume:
        return jsonify({"error": "resume is required when score_against_resume is true"}), 400

    search_logger.info("=" * 70)
    search_logger.info(f"[/api/search] keyword={keyword!r} location={location!r} "
                       f"platforms={platforms} max={max_per_platform} "
                       f"score={score_against_resume}")

    try:
        from job_automation.aggregator import JobAggregator
        jobs = JobAggregator().search(
            keyword=keyword,
            location=location,
            max_per_platform=max_per_platform,
            platforms=platforms,
            hours_old=hours_old,
        )
    except Exception as e:
        search_logger.error(f"[/api/search] aggregator error: {e}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500

    # Build per-platform breakdown from the returned jobs
    by_platform: dict[str, int] = {}
    for p in platforms:
        by_platform[p] = sum(1 for j in jobs if j.get('source') == p)

    # ── Optional scoring ───────────────────────────────────────────────────
    scored_flag = False
    if score_against_resume and resume:
        search_logger.info(f"[/api/search] scoring {len(jobs)} jobs against resume…")
        try:
            scorer = HybridScorer()
            for job in jobs:
                desc = job.get('description', '')
                if not desc:
                    job['score']        = 0
                    job['score_reason'] = 'No description available'
                    continue
                try:
                    result = scorer.score_job(resume, {
                        'title':       job.get('title', ''),
                        'company':     job.get('company', ''),
                        'description': desc,
                    })
                    job['score']             = result['final_score']
                    job['keyword_score']     = result['keyword_score']
                    job['context_score']     = result['context_score']
                    job['score_reason']      = result['recommendation']
                    job['domain_match']      = result.get('domain_match', False)
                    job['transferability']   = result.get('transferability', 'Unknown')
                except Exception as se:
                    search_logger.warning(f"[/api/search] scoring failed for {job.get('title')}: {se}")
                    job['score']        = 0
                    job['score_reason'] = 'Scoring error'
            # Sort highest score first
            jobs.sort(key=lambda j: j.get('score', 0), reverse=True)
            scored_flag = True
            search_logger.info(f"[/api/search] scoring complete")
        except Exception as e:
            search_logger.error(f"[/api/search] scorer init failed: {e}")

    # Normalise each job to the documented response fields
    clean_jobs = []
    for j in jobs:
        entry = {
            "title":       j.get("title", ""),
            "company":     j.get("company", ""),
            "location":    j.get("location", ""),
            "url":         j.get("url") or j.get("apply_link", "#"),
            "description": j.get("description", ""),
            "source":      j.get("source", ""),
            "posted":      j.get("posted", ""),
        }
        if scored_flag:
            s = j.get("score", 0)
            entry["score"]           = s
            entry["score_reason"]    = j.get("score_reason", "")
            entry["scored_by"]       = j.get("scored_by", "groq_ai")
            entry["freshness_label"] = _freshness_label(j.get("posted", ""))
        clean_jobs.append(entry)

    # Pull seen-jobs metadata if aggregator attached it to first job
    new_jobs     = jobs[0].pop("_meta_new_jobs",     len(clean_jobs)) if jobs else len(clean_jobs)
    skipped_seen = jobs[0].pop("_meta_skipped_seen", 0)              if jobs else 0

    # _freshness_label is a display helper — inline import avoids circular dep
    from job_automation.aggregator import _freshness_label
    search_logger.info(f"[/api/search] returning {len(clean_jobs)} jobs | {by_platform}")

    return jsonify({
        "jobs":          clean_jobs,
        "total":         len(clean_jobs),
        "new_jobs":      new_jobs,
        "skipped_seen":  skipped_seen,
        "by_platform":   by_platform,
        "keyword":       keyword,
        "location":      location,
        "scored":        scored_flag,
        "ranking":       "by fit score (freshness is display only)" if scored_flag else "",
    })


@app.route('/api/deep-search', methods=['POST'])
@limiter.limit("3 per hour")
def deep_search_jobs():
    """
    Thorough multi-keyword × multi-location job search.

    Expands the base keyword into up to 5 variants and searches three
    locations (city, India, Remote India) for each variant.
    Total: up to 15 search runs. Expected runtime: 15–25 minutes.

    Body (JSON):
        keyword     str   required — base job title / query
        location    str   default "Hyderabad" — your city
        resume      str   optional — full resume text; enables AI scoring
        platforms   list  default ["linkedin","indeed","naukri"]
        hours_old   int   default 168 (7 days)

    Response:
        {jobs, total, unique_after_dedup, by_platform,
         keywords_searched, locations_searched,
         search_window_days, runtime_seconds, scored}
    """
    import time as _time
    data = request.get_json(silent=True) or {}

    keyword = (data.get('keyword') or '').strip()
    if not keyword:
        return jsonify({"error": "keyword is required"}), 400

    location    = (data.get('location') or 'Hyderabad').strip()
    resume      = (data.get('resume') or '').strip()
    platforms   = data.get('platforms') or ['linkedin', 'indeed', 'naukri']
    hours_old   = int(data.get('hours_old') or 168)

    search_logger.info("=" * 70)
    search_logger.info(f"[/api/deep-search] keyword={keyword!r} location={location!r} "
                       f"platforms={platforms} hours_old={hours_old} "
                       f"scoring={'yes' if resume else 'no'}")

    # ── Groq quota pre-flight (best-effort; never blocks the search) ────────
    quota_warning: str | None = None
    if resume:
        try:
            from groq_analyzer import GroqAnalyzer
            quota = GroqAnalyzer().check_quota()
            if quota.get("status") == "ok":
                remaining = quota.get("remaining_requests", -1)
                reset_time = quota.get("reset_requests", "unknown")
                # deep search scores ~150 jobs; warn if fewer than 75 left
                estimated = 150
                if remaining != -1 and remaining < (estimated // 2):
                    quota_warning = (
                        f"Groq has {remaining} requests remaining today. "
                        f"This search will score ~{estimated} jobs. "
                        f"Some jobs may use keyword-only scoring. "
                        f"Quota resets: {reset_time} UTC. "
                        "For unlimited scoring, upgrade to Groq Developer tier "
                        "(free with credit card)."
                    )
                    search_logger.warning(f"[/api/deep-search] {quota_warning}")
        except Exception:
            pass  # quota check is best-effort; don't block the search

    try:
        from job_automation.aggregator import JobAggregator
        agg  = JobAggregator()
        jobs = agg.deep_search(
            keyword=keyword,
            location=location,
            resume_text=resume,
            platforms=platforms,
        )
        meta = agg._last_run_meta
    except Exception as e:
        search_logger.error(f"[/api/deep-search] error: {e}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500

    # Jobs are already cleaned by _clean_job_dict — pass through directly
    search_logger.info(
        f"[/api/deep-search] done — {len(jobs)} jobs | "
        f"{meta.get('by_platform',{})} | {meta.get('runtime_seconds',0)}s"
    )

    response_body = {
        "jobs":               jobs,
        "total":              len(jobs),
        "new_jobs":           meta.get("new_jobs", len(jobs)),
        "skipped_seen":       meta.get("skipped_seen", 0),
        "by_platform":        meta.get("by_platform", {}),
        "keywords_searched":  meta.get("keywords_searched", []),
        "locations_searched": meta.get("locations_searched", []),
        "window_used":        meta.get("window_used", ""),
        "runtime_seconds":    meta.get("runtime_seconds", 0),
        "scored":             meta.get("scored", False),
        "ranking":            "by fit score (freshness is display only)",
    }
    if quota_warning:
        response_body["quota_warning"] = quota_warning

    return jsonify(response_body)


@app.route('/api/search-progress/<session_id>', methods=['GET'])
def search_progress(session_id: str):
    """
    Server-Sent Events stream for real-time deep-search progress.

    GET /api/search-progress/<session_id>

    The client opens this stream before (or shortly after) starting a
    /api/deep-search with the same session_id.  The deep-search worker
    pushes text lines into _progress_queues[session_id]; this endpoint
    drains the queue and forwards them as SSE events.

    Each event: data: <progress line>\\n\\n
    Terminal event: data: __DONE__\\n\\n  (stream closes automatically)

    NOTE: deep_search does not yet push into the queue automatically —
    hooking the print output requires wrapping sys.stdout or passing the
    queue into the aggregator.  That wiring is future work; the endpoint
    is in place and clients can poll it.
    """
    import time as _time

    def generate():
        q = _progress_queues.setdefault(session_id, _queue.Queue())
        try:
            while True:
                try:
                    msg = q.get(timeout=30)
                except _queue.Empty:
                    # Send a keepalive comment so the connection stays open
                    yield ": keepalive\n\n"
                    continue

                if msg == "__DONE__":
                    yield "data: __DONE__\n\n"
                    break
                yield f"data: {msg}\n\n"
        finally:
            _progress_queues.pop(session_id, None)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
        },
    )


@app.route('/api/record-action', methods=['POST'])
def record_action():
    """
    Record what a user did with a job result.

    POST /api/record-action
    Body (JSON):
        run_id       int    required — from job._run_id (returned by deep-search)
        url          str    required — job URL (will be hashed before storage)
        title        str    optional — job title (used for pattern extraction)
        score        float  optional
        domain_match str    optional — strong|moderate|weak|unknown
        source       str    optional — linkedin|indeed|google_jobs_india
        posted       str    optional — YYYY-MM-DD
        action       str    required — applied|skipped|saved|opened_url

    Used by:
      - Future frontend apply buttons
      - Manual CSV review workflow
    """
    data = request.get_json(silent=True) or {}

    run_id = data.get("run_id")
    action = (data.get("action") or "").strip()

    if not run_id:
        return jsonify({"error": "run_id is required"}), 400
    if action not in ("applied", "skipped", "saved", "opened_url"):
        return jsonify({
            "error": "action must be one of: applied, skipped, saved, opened_url"
        }), 400

    job = {
        "url":          data.get("url", ""),
        "title":        data.get("title", ""),
        "score":        data.get("score", 0),
        "domain_match": data.get("domain_match", "unknown"),
        "source":       data.get("source", "unknown"),
        "posted":       data.get("posted", ""),
    }

    try:
        from job_automation.feedback import record_job_action
        record_job_action(int(run_id), job, action)
        app_logger.info(
            f"[/api/record-action] run={run_id} action={action} "
            f"score={job['score']} domain={job['domain_match']}"
        )
        return jsonify({"success": True, "run_id": run_id, "action": action})
    except Exception as e:
        app_logger.error(f"[/api/record-action] {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/calibration-insights', methods=['GET'])
def calibration_insights():
    """
    GET /api/calibration-insights

    Returns threshold and domain-match calibration recommendations derived
    from accumulated user feedback (applied/skipped signals).

    Response includes:
      apply_rate_by_score   — score buckets where user actually applied
      suggested_threshold   — lowest bucket with ≥30% apply rate
      domain_match_accuracy — how often each domain_match value led to apply
      total_feedback_points — number of job actions recorded so far
    """
    try:
        from job_automation.feedback import get_calibration_insights
        insights = get_calibration_insights()
        return jsonify({"success": True, "insights": insights})
    except Exception as e:
        app_logger.error(f"[/api/calibration-insights] {e}")
        return jsonify({"error": str(e)}), 500


def check_for_updates() -> None:
    """
    Compare CURRENT_VERSION against the latest GitHub release tag.
    Logs a warning if a newer version is available.
    Never raises — always silently swallowed so startup is never blocked.
    """
    import requests as _req
    try:
        r = _req.get(
            "https://api.github.com/repos/harshgarg95/job-finder-ai/releases/latest",
            timeout=3,
            headers={"Accept": "application/vnd.github+json"},
        )
        if r.status_code != 200:
            return
        latest = r.json().get("tag_name", "").lstrip("v")
        if latest and latest != CURRENT_VERSION:
            app_logger.warning(
                f"[version] Update available: v{CURRENT_VERSION} → v{latest}. "
                "Pull the latest from https://github.com/harshgarg95/job-finder-ai"
            )
        else:
            app_logger.info(f"[version] Up to date (v{CURRENT_VERSION})")
    except Exception as _ve:
        app_logger.debug(f"[version] Update check skipped: {_ve}")


@app.route('/api/version', methods=['GET'])
def api_version():
    """
    GET /api/version

    Returns current and latest version info.
    If GitHub API is unreachable, latest is null and update_available is false.

    Response:
        {
            "current":          "1.0.0",
            "latest":           "1.1.0" | null,
            "update_available": true | false
        }
    """
    import requests as _req
    latest = None
    try:
        r = _req.get(
            "https://api.github.com/repos/harshgarg95/job-finder-ai/releases/latest",
            timeout=3,
            headers={"Accept": "application/vnd.github+json"},
        )
        if r.status_code == 200:
            latest = r.json().get("tag_name", "").lstrip("v") or None
    except Exception:
        pass  # GitHub unreachable — update_available stays False

    update_available = bool(latest and latest != CURRENT_VERSION)
    return jsonify({
        "current":          CURRENT_VERSION,
        "latest":           latest,
        "update_available": update_available,
    })


@app.route('/api/reset-seen', methods=['POST'])
def reset_seen():
    """Delete seen_jobs.json so the next search returns all results fresh."""
    try:
        from job_automation.seen_jobs import clear_seen
        clear_seen()
        app_logger.info("[/api/reset-seen] seen_jobs.json cleared")
        return jsonify({
            "success": True,
            "message": "Seen jobs cleared. Next search will return all results fresh.",
        })
    except Exception as e:
        app_logger.error(f"[/api/reset-seen] {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.getenv('PORT', 8000))
    app_logger.info(f"Starting Flask on port {port}")
    check_for_updates()
    app.run(host='0.0.0.0', port=port, debug=True)
