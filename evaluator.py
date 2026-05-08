"""
evaluator.py – Critic-and-Decider LLM evaluation pipeline.

Pipeline overview
-----------------
Stage 0 – Verifier  [external source jobs only]
  Uses llm_agents.verify_company_with_cohere (Cohere Command R via GitHub Models).
  Searches DuckDuckGo for the company, asks Cohere whether it is legitimate.
  Flagged jobs are marked in the DB and excluded from suitability scoring.

Stage 1+2 – Batch Critic+Decider  [all qualifying jobs]
  Combines the recruiter critique and career-advisor verdict into a single Groq
  call per batch of LLM_BATCH_SIZE jobs.  One API call replaces the previous
  2 calls per job, reducing 429 errors by ~10×.

  The prompt includes strict anti-hallucination rules:
    • Every Job ID in the batch MUST appear in the response.
    • IDs must not be invented or modified.
    • Missing entries default to "Low" with a logged warning.

Concurrency model
-----------------
Stage 0 is concurrency-bounded by asyncio.Semaphore (one Cohere call per slot).
Stage 1+2 processes batches sequentially to avoid Groq rate limits; each batch
is a single API call so throughput is still dramatically higher than per-job mode.

Public entry points
-------------------
  batch_evaluate_jobs(...)          – in-memory enrichment (used by orchestrator)
  run_evaluation_sync(...)          – synchronous wrapper   (use from threads)
  run_evaluation_batch_guarded(...) – async wrapper         (use from FastAPI)
  is_eval_running()                 – non-destructive status check
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GROQ_MODEL       = "llama-3.1-8b-instant"
OPENAI_MODEL     = "gpt-4o"
MAX_RETRIES      = 3
BASE_BACKOFF     = 2.0
MAX_DESC_CHARS   = 500    # chars per job in batch prompt – keep context tight
MAX_RESUME_CHARS = 3_000
LLM_BATCH_SIZE   = 10    # jobs per single Groq call

# ---------------------------------------------------------------------------
# Shared run-guard
# ---------------------------------------------------------------------------

_eval_lock = threading.Lock()


def is_eval_running() -> bool:
    """Return True if an evaluation batch is currently in progress."""
    acquired = _eval_lock.acquire(blocking=False)
    if acquired:
        _eval_lock.release()
    return not acquired


# ---------------------------------------------------------------------------
# Resume loading
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def load_resume_text(pdf_path: str) -> str:
    """Extract plain text from resume.pdf using pypdf (cached after first call)."""
    try:
        import pypdf
        reader = pypdf.PdfReader(pdf_path)
        pages  = [page.extract_text() or "" for page in reader.pages]
        text   = "\n".join(pages).strip()
        if not text:
            raise ValueError("pypdf extracted empty text – PDF may be image-only")
        logger.info("[evaluator] Resume loaded: %d chars from %s", len(text), pdf_path)
        return text[:MAX_RESUME_CHARS]
    except Exception as exc:
        logger.error("[evaluator] Cannot load resume from %s: %s", pdf_path, exc)
        return ""


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _strip_fences(raw: str) -> str:
    """Remove markdown code fences before passing LLM output to json.loads()."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\n?```\s*$", "", raw)
    return raw.strip()


async def _with_backoff(coro_fn, label: str = "") -> object:
    """
    Execute an async zero-argument callable with exponential backoff on
    RateLimitError from Groq or OpenAI.
    """
    from groq  import RateLimitError as GroqRLE
    from openai import RateLimitError as OpenAIRLE

    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await coro_fn()
        except (GroqRLE, OpenAIRLE) as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                break
            wait = BASE_BACKOFF * (2 ** attempt) + __import__("random").uniform(0, 1)
            logger.warning(
                "[evaluator] %s rate-limited (attempt %d/%d). Retrying in %.1fs",
                label, attempt + 1, MAX_RETRIES, wait,
            )
            await asyncio.sleep(wait)
        except Exception:
            raise

    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Stage 0 – Company verification (external source only)
# Delegated to llm_agents.verify_company_with_cohere (Cohere Command R
# via GitHub Models) for grounded, DuckDuckGo-augmented legitimacy checks.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Stage 1+2 – Batch Critic + Decider (Groq llama-3.1-8b-instant)
# ---------------------------------------------------------------------------

_BATCH_SYSTEM = """\
You are a career advisor evaluating Singapore tech internships for a specific candidate.
Be concise, factual, and focus exclusively on technical skill match.
Respond ONLY with a valid JSON object – no markdown fences, no text outside the JSON.
You MUST return an entry for EVERY Job ID listed below. Never skip a job.\
"""

_BATCH_USER_TMPL = """\
CANDIDATE PROFILE:
{resume_text}

SCORING GUIDE:
  "High"   – Strong technical match; candidate can perform most duties immediately
              using their current skills (Python, FastAPI, Supabase, AI/ML, Java).
  "Medium" – Partial match; transferable skills exist but notable gaps remain.
  "Low"    – Poor match; fundamentally different stack or discipline.

JOBS TO EVALUATE ({n} total):
{jobs_block}

STRICT REQUIREMENTS – READ CAREFULLY:
  1. Your JSON response MUST contain exactly {n} entries, one per Job ID above.
  2. Use ONLY the provided Job IDs as keys. Do NOT invent new IDs.
  3. "llm_reasoning" must be 1-2 sentences referencing specific skills or gaps.
  4. If a description is too vague to score confidently, use "Low" with a brief note.
  5. Do NOT include any text, markdown, or commentary outside the JSON object.

Return ONLY this JSON object (string keys = Job IDs):
{{
  "<job_id>": {{"suitability_score": "High"|"Medium"|"Low", "llm_reasoning": "..."}},
  ...
}}\
"""


def _build_jobs_block(batch: list[dict], id_map: dict[str, int]) -> str:
    """
    Render a numbered job listing block for the batch prompt.

    id_map is populated in-place: key = prompt ID string → value = index in `batch`.
    Uses the job's real 'id' field when present (DB records); falls back to list
    index for pre-upsert jobs from the orchestrator.
    """
    lines: list[str] = []
    for i, job in enumerate(batch):
        key = str(job["id"]) if "id" in job else str(i)
        id_map[key] = i
        desc = (job.get("description") or "").strip()[:MAX_DESC_CHARS]
        lines.append(
            f"[Job ID: {key}]\n"
            f"Title:   {job.get('title', 'N/A')}\n"
            f"Company: {job.get('company', 'N/A')}\n"
            f"Desc:    {desc}\n"
        )
    return "\n".join(lines)


async def batch_evaluate_jobs(
    jobs: list[dict],
    resume_text: str,
    groq_key: str,
    batch_size: int = LLM_BATCH_SIZE,
) -> list[dict]:
    """
    Enrich a list of job dicts with 'suitability_score' and 'llm_reasoning'
    using a single Groq API call per batch of `batch_size` jobs.

    Jobs are modified in-place.  Jobs with descriptions shorter than 50 chars
    are skipped (scores remain unset).

    Works for both:
      • Pre-upsert jobs from the orchestrator (no 'id' field) – uses list indices.
      • DB records from run_evaluation_batch ('id' field present) – uses real IDs.

    Args:
        jobs:        Job dicts to evaluate (modified in-place).
        resume_text: Candidate resume text (passed once per function call).
        groq_key:    Groq API key.
        batch_size:  Maximum jobs per single LLM call (default 10).

    Returns:
        The same `jobs` list, with 'suitability_score' and 'llm_reasoning' added
        to each eligible dict.
    """
    from groq import AsyncGroq

    if not groq_key:
        logger.warning("[evaluator] GROQ_API_KEY not set – batch evaluation skipped")
        return jobs
    if not resume_text:
        logger.warning("[evaluator] Resume text empty – batch evaluation skipped")
        return jobs

    eligible = [j for j in jobs if len((j.get("description") or "").strip()) >= 50]
    if not eligible:
        logger.info("[evaluator] No jobs with sufficient description – nothing to evaluate")
        return jobs

    client = AsyncGroq(api_key=groq_key)
    total_scored = 0

    for batch_start in range(0, len(eligible), batch_size):
        batch  = eligible[batch_start : batch_start + batch_size]
        id_map: dict[str, int] = {}

        jobs_block = _build_jobs_block(batch, id_map)
        user_msg   = _BATCH_USER_TMPL.format(
            resume_text = resume_text,
            n           = len(batch),
            jobs_block  = jobs_block,
        )

        async def _call_batch() -> dict:
            resp = await client.chat.completions.create(
                model    = GROQ_MODEL,
                messages = [
                    {"role": "system", "content": _BATCH_SYSTEM},
                    {"role": "user",   "content": user_msg},
                ],
                temperature = 0.2,
                max_tokens  = max(600, len(batch) * 120),
            )
            raw = resp.choices[0].message.content.strip()
            logger.debug("[evaluator] Batch raw response (first 400 chars): %r", raw[:400])
            return json.loads(_strip_fences(raw))

        try:
            parsed = await _with_backoff(
                _call_batch,
                label=f"BatchEval[{batch_start}:{batch_start+len(batch)}]",
            )

            scored_in_batch = 0
            for key, result in parsed.items():
                if key not in id_map:
                    logger.debug("[evaluator] Unexpected key '%s' in batch response – ignored", key)
                    continue
                score = result.get("suitability_score", "")
                if score not in ("High", "Medium", "Low"):
                    logger.debug("[evaluator] Invalid score %r for key '%s' – skipped", score, key)
                    continue
                idx = id_map[key]
                batch[idx]["suitability_score"] = score
                batch[idx]["llm_reasoning"]     = (result.get("llm_reasoning") or "").strip()
                scored_in_batch += 1

            total_scored += scored_in_batch

            missing = set(id_map.keys()) - set(parsed.keys())
            if missing:
                logger.warning(
                    "[evaluator] Batch %d–%d: LLM skipped %d/%d jobs (IDs: %s) – left unscored",
                    batch_start, batch_start + len(batch) - 1,
                    len(missing), len(batch), sorted(missing),
                )

            logger.info(
                "[evaluator] Batch %d–%d scored %d / %d jobs",
                batch_start, batch_start + len(batch) - 1,
                scored_in_batch, len(batch),
            )

        except Exception as exc:
            logger.error(
                "[evaluator] Batch %d–%d failed: %s",
                batch_start, batch_start + len(batch) - 1,
                exc, exc_info=True,
            )

    logger.info("[evaluator] batch_evaluate_jobs complete: %d / %d jobs scored", total_scored, len(eligible))
    return jobs


# ---------------------------------------------------------------------------
# Batch runner (used by the background evaluator triggered from API / scheduler)
# ---------------------------------------------------------------------------

async def run_evaluation_batch(
    db_path:         str,
    resume_pdf_path: str,
    batch_size:      int = 50,
    concurrency:     int = 3,
) -> dict:
    """
    Core async batch runner for DB-persisted unscored jobs.

    1. Fetch up to `batch_size` unscored jobs from the DB.
    2. For external-source jobs: run Stage 0 company verification concurrently
       (Cohere Command R via llm_agents).  Flagged jobs are written to DB and
       excluded from suitability scoring.
    3. For all qualifying jobs: run batch_evaluate_jobs (one Groq call per
       LLM_BATCH_SIZE jobs).
    4. Persist suitability scores to the DB.

    Returns {"evaluated": N, "skipped": M, "errors": K}.
    """
    import database

    groq_key   = os.getenv("GROQ_API_KEY",   "")
    openai_key = os.getenv("OPENAI_API_KEY", "")   # kept for optional future use

    if not groq_key:
        logger.warning("[evaluator] GROQ_API_KEY not set – evaluation skipped")
        return {"evaluated": 0, "skipped": 0, "errors": 0}

    resume_text = load_resume_text(resume_pdf_path)
    if not resume_text:
        logger.error("[evaluator] Could not extract resume text – evaluation skipped")
        return {"evaluated": 0, "skipped": 0, "errors": 0}

    jobs = database.get_unscored_jobs(db_path, batch_size=batch_size)
    if not jobs:
        logger.info("[evaluator] No unscored jobs found – nothing to do")
        return {"evaluated": 0, "skipped": 0, "errors": 0}

    logger.info(
        "[evaluator] Background batch: %d jobs, concurrency=%d, model=%s",
        len(jobs), concurrency, GROQ_MODEL,
    )

    # ── Stage 0: company verification for external-source jobs ────────────
    external_jobs = [j for j in jobs if j.get("source") == "external"]
    internal_jobs = [j for j in jobs if j.get("source") != "external"]
    jobs_to_score = list(internal_jobs)   # will add verified external jobs below

    if external_jobs:
        from llm_agents import verify_company_with_cohere
        sem = asyncio.Semaphore(concurrency)

        async def _verify_one(job: dict) -> tuple[dict, dict]:
            async with sem:
                result = await verify_company_with_cohere(
                    company_name    = job.get("company", ""),
                    job_description = (job.get("description") or ""),
                    job_title       = job.get("title", ""),
                )
                return job, result

        verif_tasks   = [_verify_one(j) for j in external_jobs]
        verif_results = await asyncio.gather(*verif_tasks, return_exceptions=True)

        for raw in verif_results:
            if isinstance(raw, Exception):
                logger.error("[evaluator] Verification task raised: %s", raw)
                continue
            job, verif = raw
            database.update_job_verification(
                db_path,
                job["id"],
                verif["is_legitimate"],
                verif.get("verification_notes", ""),
            )
            if verif["is_legitimate"]:
                jobs_to_score.append(job)
            else:
                logger.warning(
                    "[evaluator] [id=%d] Flagged as illegitimate (confidence=%s) – skipped",
                    job["id"], verif["confidence"],
                )

    # ── Stage 1+2: batch evaluation ───────────────────────────────────────
    if not jobs_to_score:
        return {"evaluated": 0, "skipped": len(jobs), "errors": 0}

    enriched = await batch_evaluate_jobs(jobs_to_score, resume_text, groq_key)

    # ── Persist scores ────────────────────────────────────────────────────
    evaluated = skipped = errors = 0
    for job in enriched:
        score = job.get("suitability_score")
        if score:
            try:
                database.update_job_evaluation(
                    db_path,
                    job["id"],
                    score,
                    job.get("llm_reasoning", ""),
                )
                evaluated += 1
            except Exception as exc:
                logger.error("[evaluator] Failed to persist score for id=%d: %s", job["id"], exc)
                errors += 1
        else:
            skipped += 1

    logger.info(
        "[evaluator] Background batch complete: +%d evaluated, %d skipped, %d errors",
        evaluated, skipped, errors,
    )
    return {"evaluated": evaluated, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# Public entry points (guarded by the module-level lock)
# ---------------------------------------------------------------------------

def run_evaluation_sync(
    db_path:         str,
    resume_pdf_path: str,
    batch_size:      int = 50,
    concurrency:     int = 3,
) -> dict:
    """Synchronous entry point for use from background threads (APScheduler)."""
    if not _eval_lock.acquire(blocking=False):
        logger.info("[evaluator] run_evaluation_sync: already running – skipped")
        return {"evaluated": 0, "skipped": 0, "errors": 0, "reason": "already_running"}
    try:
        return asyncio.run(
            run_evaluation_batch(db_path, resume_pdf_path, batch_size, concurrency)
        )
    finally:
        _eval_lock.release()


async def run_evaluation_batch_guarded(
    db_path:         str,
    resume_pdf_path: str,
    batch_size:      int = 50,
    concurrency:     int = 3,
) -> dict:
    """Async entry point for use from FastAPI background tasks."""
    if not _eval_lock.acquire(blocking=False):
        logger.info("[evaluator] run_evaluation_batch_guarded: already running – skipped")
        return {"evaluated": 0, "skipped": 0, "errors": 0, "reason": "already_running"}
    try:
        return await run_evaluation_batch(db_path, resume_pdf_path, batch_size, concurrency)
    finally:
        _eval_lock.release()
