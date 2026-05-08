"""
database.py – SQLite setup and all CRUD helpers.

Schema history:
  v1 – core job fields
  v2 – suitability_score, llm_reasoning        (LLM evaluation)
  v3 – is_applied, company_verified,
        verification_notes                      (apply tracking + company verification)
  v4 – application_status, applied_at          (ATS status pipeline + timestamp)

All public functions accept a `db_path` argument so tests can point
them at an in-memory or temp database without touching the real file.
Safe migration is handled by migrate_db(), called automatically from init_db().
"""

import sqlite3
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    title             TEXT    NOT NULL,
    company           TEXT,
    location          TEXT,
    description       TEXT,
    url               TEXT    UNIQUE NOT NULL,
    source            TEXT    NOT NULL,
    date_posted       TEXT,
    salary            TEXT,
    scraped_at        TEXT    NOT NULL,
    suitability_score TEXT,
    llm_reasoning     TEXT,
    is_applied        INTEGER NOT NULL DEFAULT 0,
    company_verified  INTEGER,
    verification_notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_source     ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_date       ON jobs(date_posted DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_scraped_at ON jobs(scraped_at  DESC);
"""

# Valid application pipeline states.
VALID_STATUSES: frozenset[str] = frozenset({
    "unapplied",
    "applied",
    "screening",
    "interview",
    "offer",
    "rejection",
    "withdrawn",
    "no response",
})

# All columns added after v1 – migrate_db() adds any that are missing.
_MIGRATION_COLUMNS: list[tuple[str, str]] = [
    ("suitability_score",   "TEXT"),
    ("llm_reasoning",       "TEXT"),
    ("is_applied",          "INTEGER NOT NULL DEFAULT 0"),
    ("company_verified",    "INTEGER"),
    ("verification_notes",  "TEXT"),
    # v4 – ATS pipeline
    ("application_status",  "TEXT NOT NULL DEFAULT 'unapplied'"),
    ("applied_at",          "TEXT"),
]


def get_connection(db_path: str) -> sqlite3.Connection:
    """Return a connection with row_factory set to Row for dict-like access."""
    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate_db(db_path: str) -> None:
    """
    Idempotently add any schema columns that don't yet exist.
    Safe to call on both new and old databases.
    """
    with get_connection(db_path) as conn:
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for col, typedef in _MIGRATION_COLUMNS:
            if col not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typedef}")
                logger.info("Migration: added column '%s %s' to jobs", col, typedef)

        # These indexes reference migration columns, so they must be created
        # here (after the ALTER TABLEs) rather than in the DDL executescript.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_score      ON jobs(suitability_score)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_applied    ON jobs(is_applied)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_app_status ON jobs(application_status)"
        )

        # v4 data migration: sync pre-existing is_applied=1 rows into the
        # new application_status column so the ATS view shows them correctly.
        conn.execute("""
            UPDATE jobs
            SET    application_status = 'applied'
            WHERE  is_applied = 1
              AND  application_status = 'unapplied'
        """)

        conn.commit()


def init_db(db_path: str) -> None:
    """Create tables/indexes if they don't exist, then run migration."""
    with get_connection(db_path) as conn:
        conn.executescript(DDL)
    migrate_db(db_path)
    logger.info("Database initialised at %s", db_path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _days_ago(date_str: Optional[str]) -> Optional[int]:
    """Return whole days elapsed since date_str, or None if unparseable."""
    if not date_str:
        return None
    try:
        from dateutil.parser import parse as _parse
        d = _parse(date_str)
        now = datetime.now(timezone.utc)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return max(0, (now - d).days)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def upsert_job(conn: sqlite3.Connection, job: dict) -> bool:
    """
    Insert a job row.  If the URL already exists, skip (no update).
    Returns True if a new row was inserted, False if it was a duplicate.

    When the job dict includes 'suitability_score' and 'llm_reasoning' keys
    (populated by the inline batch evaluator in the orchestrator), those values
    are persisted in the same INSERT so no separate UPDATE call is needed.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO jobs
                (title, company, location, description, url, source,
                 date_posted, salary, scraped_at,
                 suitability_score, llm_reasoning)
            VALUES
                (:title, :company, :location, :description, :url, :source,
                 :date_posted, :salary, :scraped_at,
                 :suitability_score, :llm_reasoning)
            """,
            {
                "title":             job.get("title", "").strip(),
                "company":           (job.get("company") or "").strip(),
                "location":          (job.get("location") or "").strip() or None,
                "description":       (job.get("description") or "").strip() or None,
                "url":               job.get("url", "").strip(),
                "source":            job.get("source", "").strip(),
                "date_posted":       job.get("date_posted"),
                "salary":            job.get("salary"),
                "scraped_at":        now,
                "suitability_score": job.get("suitability_score") or None,
                "llm_reasoning":     (job.get("llm_reasoning") or "").strip() or None,
            },
        )
        inserted = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        return inserted == 1
    except sqlite3.Error as exc:
        logger.error("DB upsert failed for url=%s: %s", job.get("url"), exc)
        conn.rollback()
        return False


def bulk_upsert(db_path: str, jobs: list[dict]) -> tuple[int, int]:
    """Insert a list of job dicts.  Returns (inserted, skipped) counts."""
    inserted = skipped = 0
    with get_connection(db_path) as conn:
        for job in jobs:
            if upsert_job(conn, job):
                inserted += 1
            else:
                skipped += 1
    logger.info("Bulk upsert: +%d new, %d skipped", inserted, skipped)
    return inserted, skipped


def update_job_evaluation(
    db_path: str,
    job_id: int,
    suitability_score: str,
    llm_reasoning: str,
) -> None:
    """Persist the LLM evaluation result for a single job."""
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET suitability_score = ?, llm_reasoning = ? WHERE id = ?",
            (suitability_score, llm_reasoning, job_id),
        )
        conn.commit()


def update_job_status(
    db_path: str,
    job_id:  int,
    status:  str,
) -> Optional[dict]:
    """
    Update the application_status for a job and keep is_applied in sync.

    Rules:
      • First transition away from 'unapplied' → sets applied_at to now.
      • Returning to 'unapplied' → clears applied_at.
      • Subsequent status changes preserve the original applied_at timestamp.

    Returns the updated fields dict, or None if the job_id does not exist.
    Raises ValueError for unrecognised status values.
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status {status!r}. "
            f"Valid values: {sorted(VALID_STATUSES)}"
        )

    now = datetime.now(timezone.utc).isoformat()

    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT application_status, applied_at FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()

        if not row:
            return None

        current_status   = row["application_status"]
        current_applied_at = row["applied_at"]

        # Stamp applied_at on the first transition out of 'unapplied'
        if current_status == "unapplied" and status != "unapplied" and not current_applied_at:
            new_applied_at = now
        elif status == "unapplied":
            new_applied_at = None        # clear when removing from tracking
        else:
            new_applied_at = current_applied_at  # preserve original stamp

        is_applied = 0 if status == "unapplied" else 1

        conn.execute(
            """
            UPDATE jobs
            SET    application_status = ?,
                   applied_at         = ?,
                   is_applied         = ?
            WHERE  id = ?
            """,
            (status, new_applied_at, is_applied, job_id),
        )
        conn.commit()

    logger.info(
        "[db] Job %d status: %s → %s (applied_at=%s)",
        job_id, current_status, status, new_applied_at,
    )
    return {
        "id":                 job_id,
        "application_status": status,
        "applied_at":         new_applied_at,
        "is_applied":         bool(is_applied),
    }


def update_job_applied(db_path: str, job_id: int, is_applied: bool) -> None:
    """
    Toggle the applied state (legacy helper – wraps update_job_status).
    Sets status to 'applied' when marking as applied, 'unapplied' when removing.
    """
    update_job_status(db_path, job_id, "applied" if is_applied else "unapplied")


def update_job_verification(
    db_path: str,
    job_id: int,
    company_verified: bool,
    verification_notes: str,
) -> None:
    """Persist the company legitimacy verification result."""
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET company_verified = ?, verification_notes = ? WHERE id = ?",
            (1 if company_verified else 0, verification_notes, job_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def get_jobs(
    db_path:     str,
    search:      Optional[str]  = None,
    source:      Optional[str]  = None,
    score:       Optional[str]  = None,
    is_applied:  Optional[bool] = None,
    app_status:  Optional[str]  = None,
    limit:       int = 100,
    offset:      int = 0,
) -> list[dict]:
    """
    Return jobs as a list of dicts, newest first.

    Filters:
    - search:     case-insensitive substring on title OR company
    - source:     exact match on source column
    - score:      "High" | "Medium" | "Low" | "unscored"
    - is_applied: True → only applied; False → only not-applied (legacy)
    - app_status: "active"            → all non-unapplied jobs
                  any VALID_STATUS    → exact match on application_status
    """
    conditions: list[str] = []
    params: list = []

    if search:
        conditions.append("(LOWER(title) LIKE ? OR LOWER(company) LIKE ?)")
        term = f"%{search.lower()}%"
        params.extend([term, term])

    if source:
        conditions.append("source = ?")
        params.append(source)

    if score == "unscored":
        conditions.append("suitability_score IS NULL")
    elif score in ("High", "Medium", "Low"):
        conditions.append("suitability_score = ?")
        params.append(score)

    if app_status == "active":
        conditions.append("application_status != 'unapplied'")
    elif app_status:
        conditions.append("application_status = ?")
        params.append(app_status)
    elif is_applied is True:
        conditions.append("is_applied = 1")
    elif is_applied is False:
        conditions.append("is_applied = 0")

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = f"""
        SELECT id, title, company, location, description, url,
               source, date_posted, salary, scraped_at,
               suitability_score, llm_reasoning,
               is_applied, company_verified, verification_notes,
               application_status, applied_at
        FROM   jobs
        {where_clause}
        ORDER  BY COALESCE(date_posted, scraped_at) DESC
        LIMIT  ? OFFSET ?
    """
    params.extend([limit, offset])

    with get_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()

    result = []
    for r in rows:
        row = dict(r)
        row["days_since_posted"]  = _days_ago(row.get("date_posted"))
        row["is_applied"]         = bool(row.get("is_applied", 0))
        row["application_status"] = row.get("application_status") or "unapplied"
        result.append(row)
    return result


def get_job_by_id(db_path: str, job_id: int) -> Optional[dict]:
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["days_since_posted"] = _days_ago(result.get("date_posted"))
    result["is_applied"] = bool(result.get("is_applied", 0))
    return result


def get_unscored_jobs(db_path: str, batch_size: int = 50) -> list[dict]:
    """
    Return jobs that have not been evaluated yet.
    Only includes rows with a non-trivial description (>50 chars).
    """
    sql = """
        SELECT id, title, company, location, description, url, source, date_posted, salary
        FROM   jobs
        WHERE  suitability_score IS NULL
          AND  description IS NOT NULL
          AND  LENGTH(TRIM(description)) > 50
        ORDER  BY scraped_at DESC
        LIMIT  ?
    """
    with get_connection(db_path) as conn:
        rows = conn.execute(sql, (batch_size,)).fetchall()
    return [dict(r) for r in rows]


def get_stats(db_path: str) -> dict:
    """Return per-source counts, evaluation summary, and last scrape timestamp."""
    with get_connection(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

        by_source = conn.execute(
            "SELECT source, COUNT(*) as count FROM jobs GROUP BY source"
        ).fetchall()

        last_scrape = conn.execute(
            "SELECT MAX(scraped_at) FROM jobs"
        ).fetchone()[0]

        eval_rows = conn.execute(
            """SELECT suitability_score, COUNT(*) as count
               FROM   jobs
               WHERE  suitability_score IS NOT NULL
               GROUP  BY suitability_score"""
        ).fetchall()

        unscored = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE suitability_score IS NULL"
        ).fetchone()[0]

        applied_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE is_applied = 1"
        ).fetchone()[0]

        flagged_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE company_verified = 0"
        ).fetchone()[0]

        # ATS pipeline breakdown (excludes 'unapplied')
        status_rows = conn.execute(
            """SELECT application_status, COUNT(*) AS count
               FROM   jobs
               WHERE  application_status != 'unapplied'
               GROUP  BY application_status"""
        ).fetchall()

    return {
        "total":            total,
        "by_source":        {r["source"]: r["count"] for r in by_source},
        "last_scraped_at":  last_scrape,
        "eval_scores":      {r["suitability_score"]: r["count"] for r in eval_rows},
        "unscored":         unscored,
        "applied_count":    applied_count,
        "flagged_count":    flagged_count,
        "by_app_status":    {r["application_status"]: r["count"] for r in status_rows},
    }
