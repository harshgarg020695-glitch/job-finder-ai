"""
feedback.py — Local feedback capture and self-learning calibration.

No PII. No network calls. Runs always.

Tables:
  search_runs       — one row per deep_search() call (hashed keyword)
  job_actions       — what user did with each job (applied/skipped/saved)
  score_calibration — aggregate apply-rate snapshots (written by calibrate())
"""

import sqlite3
import hashlib
from datetime import datetime

FEEDBACK_DB = "feedback.db"


def _hash(value: str) -> str:
    """One-way hash — can't recover original value."""
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


# ── Schema ─────────────────────────────────────────────────────────────────

def init_feedback_db() -> None:
    conn = sqlite3.connect(FEEDBACK_DB)
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS search_runs (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp        TEXT,
        keyword_hash     TEXT,
        location         TEXT,
        profile_level    TEXT,
        profile_domain   TEXT,
        total_jobs       INTEGER,
        jobs_above_70    INTEGER,
        domain_strong    INTEGER,
        domain_moderate  INTEGER,
        domain_weak      INTEGER,
        window_used      TEXT,
        runtime_seconds  REAL,
        groq_model       TEXT,
        score_mean       REAL,
        score_p25        REAL,
        score_p75        REAL
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS job_actions (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp         TEXT,
        run_id            INTEGER,
        url_hash          TEXT,
        title_pattern     TEXT,
        company_size_guess TEXT,
        score             REAL,
        domain_match      TEXT,
        source            TEXT,
        days_since_posted INTEGER,
        action            TEXT,
        job_title         TEXT,
        company           TEXT,
        FOREIGN KEY (run_id) REFERENCES search_runs(id)
    )""")

    # Safe migration — adds columns to existing DBs without data loss
    for col, coltype in [("job_title", "TEXT"), ("company", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE job_actions ADD COLUMN {col} {coltype}")
        except Exception:
            pass

    c.execute("""
    CREATE TABLE IF NOT EXISTS score_calibration (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp     TEXT,
        profile_domain TEXT,
        score_bucket  TEXT,
        applied_count INTEGER,
        skipped_count INTEGER,
        apply_rate    REAL
    )""")

    conn.commit()
    conn.close()


# ── Write helpers ──────────────────────────────────────────────────────────

def save_search_run(
    keyword: str,
    location: str,
    jobs: list,
    profile: dict,
    runtime: float,
    window: str,
) -> int:
    """
    Persist a search run summary. Returns run_id for linking job actions.
    Keyword is one-way hashed — original cannot be recovered.
    """
    init_feedback_db()
    conn = sqlite3.connect(FEEDBACK_DB)
    c = conn.cursor()

    scores = sorted([float(j.get("score", 0) or 0) for j in jobs])
    n = len(scores)

    c.execute("""
    INSERT INTO search_runs (
        timestamp, keyword_hash, location, profile_level, profile_domain,
        total_jobs, jobs_above_70, domain_strong, domain_moderate, domain_weak,
        window_used, runtime_seconds, groq_model,
        score_mean, score_p25, score_p75
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now().isoformat(),
        _hash(keyword),
        location,
        profile.get("level", "unknown"),
        profile.get("primary_domain", "unknown")[:50],
        n,
        sum(1 for s in scores if s >= 70),
        sum(1 for j in jobs if j.get("domain_match") == "strong"),
        sum(1 for j in jobs if j.get("domain_match") == "moderate"),
        sum(1 for j in jobs if j.get("domain_match") == "weak"),
        window,
        round(runtime, 1),
        "llama-3.1-8b-instant",
        sum(scores) / n if n else 0,
        scores[n // 4]     if n >= 4 else 0,
        scores[3 * n // 4] if n >= 4 else 0,
    ))

    run_id = c.lastrowid
    conn.commit()
    conn.close()
    return run_id


def record_job_action(run_id: int, job: dict, action: str) -> None:
    """
    Record what the user did with a job.

    action: 'applied' | 'skipped' | 'saved' | 'opened_url'

    Call this when:
      - User marks a job in the CSV export (applied / skipped)
      - Frontend hits POST /api/record-action
    """
    init_feedback_db()

    # Anonymised title pattern — keeps role category, drops proper nouns
    title = (job.get("title") or "").lower()
    title_pattern = (
        "director"    if "director"    in title else
        "manager"     if "manager"     in title else
        "consultant"  if "consultant"  in title else
        "engineer"    if "engineer"    in title else
        "analyst"     if "analyst"     in title else
        "architect"   if "architect"   in title else
        "lead"        if "lead"        in title else
        "other"
    )

    posted = job.get("posted", "")
    try:
        days_old = (datetime.now() - datetime.strptime(posted[:10], "%Y-%m-%d")).days
    except Exception:
        days_old = -1

    conn = sqlite3.connect(FEEDBACK_DB)
    c = conn.cursor()
    c.execute("""
    INSERT INTO job_actions (
        timestamp, run_id, url_hash, title_pattern,
        score, domain_match, source, days_since_posted, action,
        job_title, company
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now().isoformat(),
        run_id,
        _hash(job.get("url", "")),
        title_pattern,
        float(job.get("score", 0) or 0),
        job.get("domain_match", "unknown"),
        job.get("source", "unknown"),
        days_old,
        action,
        (job.get("title") or "")[:200],
        (job.get("company") or "")[:100],
    ))
    conn.commit()
    conn.close()


# ── Read / analysis ────────────────────────────────────────────────────────

def get_calibration_insights() -> dict:
    """
    Analyse accumulated feedback and return actionable recommendations.

    Returns:
      apply_rate_by_score   — dict: score bucket → {applied, skipped, apply_rate}
      suggested_threshold   — lowest bucket with ≥30% apply rate
      domain_match_accuracy — dict: domain_match value → {apply_rate, total}
      total_feedback_points — int
    """
    init_feedback_db()
    conn = sqlite3.connect(FEEDBACK_DB)
    c = conn.cursor()

    # ── Apply rate by score bucket ─────────────────────────────────────────
    c.execute("""
        SELECT
            CASE
                WHEN score >= 90 THEN '90-100'
                WHEN score >= 80 THEN '80-89'
                WHEN score >= 75 THEN '75-79'
                WHEN score >= 70 THEN '70-74'
                WHEN score >= 65 THEN '65-69'
                ELSE 'below-65'
            END AS bucket,
            SUM(CASE WHEN action = 'applied' THEN 1 ELSE 0 END) AS applied,
            SUM(CASE WHEN action = 'skipped' THEN 1 ELSE 0 END) AS skipped,
            COUNT(*) AS total
        FROM job_actions
        GROUP BY bucket
        ORDER BY bucket DESC
    """)
    apply_rates: dict = {}
    for bucket, applied, skipped, total in c.fetchall():
        if total >= 3:
            apply_rates[bucket] = {
                "applied":    applied,
                "skipped":    skipped,
                "apply_rate": round(applied / total, 2),
            }

    # Recommended threshold — lowest bucket with ≥30% apply rate
    threshold_suggestion = 70  # safe default
    for bucket in sorted(apply_rates.keys(), reverse=True):
        if apply_rates[bucket]["apply_rate"] >= 0.3:
            lo = int(bucket.split("-")[0]) if "-" in bucket else 70
            threshold_suggestion = lo
            break

    # ── Domain match accuracy ─────────────────────────────────────────────
    c.execute("""
        SELECT domain_match,
               SUM(CASE WHEN action = 'applied' THEN 1 ELSE 0 END) AS applied,
               COUNT(*) AS total
        FROM job_actions
        GROUP BY domain_match
    """)
    domain_accuracy: dict = {}
    for domain, applied, total in c.fetchall():
        if total >= 3:
            domain_accuracy[domain] = {
                "apply_rate": round(applied / total, 2),
                "total":      total,
            }

    # ── Total data points ─────────────────────────────────────────────────
    c.execute("SELECT COUNT(*) FROM job_actions")
    total_points = c.fetchone()[0]

    conn.close()

    return {
        "apply_rate_by_score":   apply_rates,
        "suggested_threshold":   threshold_suggestion,
        "domain_match_accuracy": domain_accuracy,
        "total_feedback_points": total_points,
    }


def get_my_stats() -> dict:
    """
    Aggregate user behaviour stats across all recorded job actions.

    Returns a summary suitable for the /api/my-stats endpoint:
      total_jobs_seen          — total action records
      total_applied            — applied actions
      total_skipped            — skipped actions
      apply_rate               — applied / (applied + skipped)
      top_applied_companies    — top 5 companies by applied count
      top_skipped_title_patterns — top 5 words in skipped job titles
      score_distribution       — mean/min/max per action
      searches_run             — total search_runs rows
      last_search              — ISO date of most recent run
    """
    init_feedback_db()
    conn = sqlite3.connect(FEEDBACK_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # ── Totals ────────────────────────────────────────────────────────────
    c.execute("SELECT COUNT(*) FROM job_actions")
    total_seen = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM job_actions WHERE action = 'applied'")
    total_applied = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM job_actions WHERE action = 'skipped'")
    total_skipped = c.fetchone()[0]

    decisive = total_applied + total_skipped
    apply_rate = round(total_applied / decisive, 3) if decisive else 0.0

    # ── Top applied companies ─────────────────────────────────────────────
    c.execute("""
        SELECT company, COUNT(*) AS cnt
        FROM job_actions
        WHERE action = 'applied' AND company IS NOT NULL AND company != ''
        GROUP BY company
        ORDER BY cnt DESC
        LIMIT 5
    """)
    top_applied_companies = [r["company"] for r in c.fetchall()]

    # ── Top words in skipped job titles ──────────────────────────────────
    # Tokenise all skipped titles → frequency-rank meaningful words
    _STOPWORDS = {
        "a","an","the","and","or","of","in","for","to","at","on","with","is","are",
        "be","by","as","it","we","you","this","that","–","—","-","&","(",")","/",
        "i","ii","iii","iv","us","uk","india","hyderabad","bangalore","remote",
        "level","senior","junior","lead","associate","mid","role","position","job",
        "full","time","part","contract","manager","management",
    }
    c.execute("""
        SELECT job_title FROM job_actions
        WHERE action = 'skipped' AND job_title IS NOT NULL AND job_title != ''
    """)
    word_freq: dict = {}
    for row in c.fetchall():
        for word in row["job_title"].replace("-", " ").replace("/", " ").split():
            w = word.strip("(),.:").lower()
            if len(w) >= 3 and w not in _STOPWORDS:
                word_freq[w] = word_freq.get(w, 0) + 1
    top_skipped = [w for w, _ in sorted(word_freq.items(), key=lambda x: -x[1])[:5]]

    # ── Score distribution per action ────────────────────────────────────
    score_dist: dict = {}
    for action in ("applied", "skipped", "saved"):
        c.execute("""
            SELECT AVG(score) AS mean, MIN(score) AS mn, MAX(score) AS mx,
                   COUNT(*) AS n
            FROM job_actions WHERE action = ?
        """, (action,))
        row = c.fetchone()
        if row and row["n"] and row["n"] > 0:
            score_dist[action] = {
                "mean":  round(float(row["mean"] or 0), 1),
                "min":   round(float(row["mn"]   or 0), 1),
                "max":   round(float(row["mx"]   or 0), 1),
                "count": row["n"],
            }

    # ── Search run stats ──────────────────────────────────────────────────
    c.execute("SELECT COUNT(*) FROM search_runs")
    searches_run = c.fetchone()[0]

    c.execute("SELECT MAX(timestamp) FROM search_runs")
    last_ts = c.fetchone()[0]
    last_search = last_ts[:10] if last_ts else None

    conn.close()

    return {
        "total_jobs_seen":             total_seen,
        "total_applied":               total_applied,
        "total_skipped":               total_skipped,
        "apply_rate":                  apply_rate,
        "top_applied_companies":       top_applied_companies,
        "top_skipped_title_patterns":  top_skipped,
        "score_distribution":          score_dist,
        "searches_run":                searches_run,
        "last_search":                 last_search,
    }


def get_action_history() -> list:
    """
    Return all job_actions rows for CSV export.
    Columns: timestamp, job_title, company, score, action, run_id
    """
    init_feedback_db()
    conn = sqlite3.connect(FEEDBACK_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT timestamp, job_title, company, score, action, run_id
        FROM job_actions
        ORDER BY timestamp DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_run_history(last_n: int = 10) -> list:
    """Return the last N search_runs rows (newest first)."""
    init_feedback_db()
    conn = sqlite3.connect(FEEDBACK_DB)
    c = conn.cursor()
    c.execute("""
        SELECT id, timestamp, location, profile_level, profile_domain,
               total_jobs, jobs_above_70, window_used, runtime_seconds, score_mean
        FROM search_runs
        ORDER BY id DESC
        LIMIT ?
    """, (last_n,))
    rows = c.fetchall()
    conn.close()
    return rows
