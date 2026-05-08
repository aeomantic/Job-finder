"""
orchestrator.py – Main scraping orchestration script.

Runs all four scrapers, gathers their results concurrently, and persists
the unified result set in SQLite. After scraping completes, kicks off the
LLM evaluation batch in a separate daemon thread so the scheduler's
run-time is not extended by API latency.

Usage:
    python orchestrator.py           # run once and exit
    python orchestrator.py --run-now # alias for above
"""

import asyncio
import argparse
import inspect
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging – must be configured before any project imports that use logging
# ---------------------------------------------------------------------------

LOG_FILE  = os.getenv("LOG_FILE",  "./logs/scraper.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else ".", exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------

import database
import scraper_jobstreet
import scraper_mycareersfuture
import scraper_internsg
import scraper_careers_gov
import scraper_external
from query_generator import generate_search_queries as generate_queries

DB_PATH          = os.getenv("DB_PATH",          "./data/jobs.db")
RESUME_PDF_PATH  = os.getenv("RESUME_PDF_PATH",  "./resume.pdf")
EVAL_BATCH_SIZE  = int(os.getenv("EVAL_BATCH_SIZE",  "50"))
EVAL_CONCURRENCY = int(os.getenv("EVAL_CONCURRENCY", "3"))
DEFAULT_SEARCH_TERMS: list[str] = [
    "Software Engineering Intern",
    "Backend Intern",
]

# External-source jobs require Stage 0 company verification (Cohere) before
# suitability scoring.  The inline batch evaluator skips them here; the
# background evaluator picks them up with proper verification afterwards.
_INLINE_EVAL_SKIP_SOURCES = frozenset({"external"})


@dataclass(slots=True)
class ScraperRunResult:
    source: str
    jobs: list[dict]
    elapsed: float
    error: Exception | None = None


# ---------------------------------------------------------------------------
# Evaluation helper
# ---------------------------------------------------------------------------

def _launch_eval_background() -> None:
    """
    Spawn a daemon thread that runs the LLM evaluation batch.

    Using a daemon thread means:
    - The scraper run returns to the scheduler immediately (no blocking).
    - If the server shuts down mid-evaluation, the thread exits cleanly.
    - The module-level lock in evaluator.py prevents duplicate runs even if
      a manual API trigger fires while this thread is still active.
    """
    if not os.path.exists(RESUME_PDF_PATH):
        logger.warning(
            "[orchestrator] resume.pdf not found at '%s' – LLM evaluation skipped. "
            "Set RESUME_PDF_PATH in .env to enable it.",
            RESUME_PDF_PATH,
        )
        return

    from evaluator import run_evaluation_sync, is_eval_running

    if is_eval_running():
        logger.info("[orchestrator] LLM evaluation already running – skipping new trigger")
        return

    def _run() -> None:
        logger.info("[orchestrator] LLM evaluation thread started")
        summary = run_evaluation_sync(
            db_path         = DB_PATH,
            resume_pdf_path = RESUME_PDF_PATH,
            batch_size      = EVAL_BATCH_SIZE,
            concurrency     = EVAL_CONCURRENCY,
        )
        logger.info("[orchestrator] LLM evaluation thread finished: %s", summary)

    t = threading.Thread(target=_run, name="llm-evaluator", daemon=True)
    t.start()


def _clean_search_terms(search_terms: Any) -> list[str]:
    if not isinstance(search_terms, list):
        return []

    cleaned = [term.strip() for term in search_terms if isinstance(term, str) and term.strip()]
    return cleaned


async def _resolve_search_terms() -> list[str]:
    try:
        search_terms = generate_queries(RESUME_PDF_PATH)
    except Exception as exc:
        logger.warning(
            "[orchestrator] generate_queries() failed (%s) – using defaults: %s",
            exc,
            DEFAULT_SEARCH_TERMS,
        )
        return DEFAULT_SEARCH_TERMS.copy()

    cleaned = _clean_search_terms(search_terms)
    if not cleaned:
        logger.warning(
            "[orchestrator] generate_queries() returned no usable terms – using defaults: %s",
            DEFAULT_SEARCH_TERMS,
        )
        return DEFAULT_SEARCH_TERMS.copy()

    return cleaned


def _build_scraper_kwargs(module: Any, search_terms: list[str], base_kwargs: dict[str, Any]) -> dict[str, Any]:
    kwargs = dict(base_kwargs)
    params = inspect.signature(module.scrape).parameters

    if "search_terms" in params:
        kwargs["search_terms"] = search_terms
    elif "search_term" in params:
        kwargs["search_term"] = search_terms[0]

    return kwargs


async def _run_scraper(module: Any, kwargs: dict[str, Any]) -> ScraperRunResult:
    source_name = getattr(module, "SOURCE", module.__name__)
    started_at = time.perf_counter()

    try:
        jobs = await asyncio.to_thread(module.scrape, **kwargs)
        elapsed = time.perf_counter() - started_at

        if not isinstance(jobs, list):
            raise TypeError(f"{source_name}.scrape() returned {type(jobs).__name__}, expected list")

        return ScraperRunResult(source=source_name, jobs=jobs, elapsed=elapsed)
    except Exception as exc:
        elapsed = time.perf_counter() - started_at
        return ScraperRunResult(source=source_name, jobs=[], elapsed=elapsed, error=exc)


async def _inline_batch_eval(
    results: list["ScraperRunResult"],
    resume_text: str,
) -> None:
    """
    Run batch LLM evaluation on each scraper's job list in-place, before upsert.

    One Groq call is made per LLM_BATCH_SIZE jobs per scraper, replacing the
    old per-job two-call approach.  This function modifies each result's job
    dicts in-place so that bulk_upsert saves the scores on initial INSERT.

    External-source jobs are skipped here because they need Stage 0 Cohere
    verification first.  The background evaluator handles them properly after
    the upsert.
    """
    from evaluator import batch_evaluate_jobs

    groq_key = os.getenv("GROQ_API_KEY", "")
    if not groq_key:
        logger.info("[orchestrator] GROQ_API_KEY not set – inline evaluation skipped")
        return
    if not resume_text:
        logger.info("[orchestrator] Resume text unavailable – inline evaluation skipped")
        return

    for result in results:
        if result.error or not result.jobs:
            continue
        if result.source in _INLINE_EVAL_SKIP_SOURCES:
            logger.info(
                "[orchestrator] Skipping inline eval for '%s' (handled by background evaluator)",
                result.source,
            )
            continue

        eligible = [j for j in result.jobs if len((j.get("description") or "").strip()) >= 50]
        if not eligible:
            logger.info("[orchestrator] [%s] No eligible jobs for inline eval", result.source)
            continue

        logger.info(
            "[orchestrator] Inline batch eval for '%s': %d / %d eligible jobs",
            result.source, len(eligible), len(result.jobs),
        )
        try:
            # batch_evaluate_jobs enriches dicts in-place and returns the same list
            await batch_evaluate_jobs(eligible, resume_text, groq_key)
            scored = sum(1 for j in eligible if j.get("suitability_score"))
            logger.info(
                "[orchestrator] [%s] Inline eval complete: %d / %d scored",
                result.source, scored, len(eligible),
            )
        except Exception as exc:
            logger.error(
                "[orchestrator] Inline eval failed for '%s': %s",
                result.source, exc, exc_info=True,
            )


async def _run_all_scrapers_async() -> dict:
    """
    Phase 1 – All scrapers run concurrently.
    Phase 2 – Inline batch LLM evaluation enriches each scraper's job list in-place.
    Phase 3 – Bulk upsert saves jobs with scores already attached.
    Phase 4 – Background evaluator catches external-source jobs and any misses.

    Returns a summary dict:
        {source: {"inserted": N, "skipped": N, "errors": N}}
    """
    summary: dict[str, dict[str, int]] = {}
    started_at = datetime.now(timezone.utc).isoformat()

    logger.info("=" * 60)
    logger.info("Orchestrator run started at %s", started_at)
    logger.info("=" * 60)

    database.init_db(DB_PATH)

    search_terms = await _resolve_search_terms()
    logger.info("[orchestrator] Search queries for this run: %s", search_terms)

    # ── Phase 1: concurrent scraping ─────────────────────────────────────
    scraper_specs: list[tuple[Any, dict[str, Any]]] = [
        (scraper_mycareersfuture, {"max_pages": 10}),
        (scraper_jobstreet,       {"results_wanted": 100}),
        (scraper_internsg,        {"max_pages": 5}),
        (scraper_careers_gov,     {"max_pages": 5}),
        (scraper_external,        {}),
    ]

    tasks = [
        _run_scraper(module, _build_scraper_kwargs(module, search_terms, base_kwargs))
        for module, base_kwargs in scraper_specs
    ]
    results: list[ScraperRunResult] = await asyncio.gather(*tasks)

    for result in results:
        if result.error is not None:
            logger.error(
                "Scraper %s raised an unhandled exception after %.1fs: %s",
                result.source, result.elapsed, result.error,
                exc_info=(type(result.error), result.error, result.error.__traceback__),
            )
            summary[result.source] = {"inserted": 0, "skipped": 0, "errors": 1}
        elif not result.jobs:
            logger.warning("[%s] Returned 0 jobs in %.1fs", result.source, result.elapsed)
            summary[result.source] = {"inserted": 0, "skipped": 0, "errors": 0}

    # ── Phase 2: inline batch evaluation (pre-upsert) ────────────────────
    # Load resume once; evaluator also caches it via @lru_cache, so this
    # call is effectively free on the second access.
    resume_text = ""
    if os.path.exists(RESUME_PDF_PATH):
        try:
            from evaluator import load_resume_text
            resume_text = load_resume_text(RESUME_PDF_PATH)
        except Exception as exc:
            logger.warning("[orchestrator] Could not load resume for inline eval: %s", exc)

    good_results = [r for r in results if not r.error and r.jobs]
    await _inline_batch_eval(good_results, resume_text)

    # ── Phase 3: bulk upsert (scores already attached to job dicts) ──────
    for result in good_results:
        if result.source in summary:
            continue   # already recorded an error above
        inserted, skipped = database.bulk_upsert(DB_PATH, result.jobs)
        logger.info(
            "[%s] Done in %.1fs -> +%d new, %d skipped (fetched: %d, pre-scored: %d)",
            result.source, result.elapsed, inserted, skipped, len(result.jobs),
            sum(1 for j in result.jobs if j.get("suitability_score")),
        )
        summary[result.source] = {"inserted": inserted, "skipped": skipped, "errors": 0}

    finished_at = datetime.now(timezone.utc).isoformat()
    logger.info("=" * 60)
    logger.info("Orchestrator run finished at %s", finished_at)
    logger.info("Summary: %s", summary)
    logger.info("=" * 60)

    # ── Phase 4: background eval for external jobs + any misses ──────────
    _launch_eval_background()
    return summary


# ---------------------------------------------------------------------------
# Main scraper pipeline
# ---------------------------------------------------------------------------

def run_all_scrapers() -> dict:
    """
    Execute all scrapers concurrently and persist the combined results.

    This remains a synchronous API for scheduler.py, api.py, and CLI callers.
    """
    return asyncio.run(_run_all_scrapers_async())


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Run the internship scraper pipeline.")
    parser.add_argument(
        "--run-now", action="store_true",
        help="Run all scrapers immediately then evaluate unscored jobs.",
    )
    parser.add_argument(
        "--eval-only", action="store_true",
        help="Skip scraping – only run LLM evaluation on unscored jobs.",
    )
    parser.add_argument(
        "--queries-only", action="store_true",
        help="Dry-run: generate and print the search queries from resume.pdf, then exit.",
    )
    args = parser.parse_args()

    if args.queries_only:
        queries = generate_queries(RESUME_PDF_PATH)
        print("\nGenerated search queries:")
        for i, q in enumerate(queries, 1):
            print(f"  {i}. {q}")
        return

    if args.eval_only:
        from evaluator import run_evaluation_sync
        logger.info("Eval-only mode")
        result = run_evaluation_sync(
            db_path         = DB_PATH,
            resume_pdf_path = RESUME_PDF_PATH,
            batch_size      = EVAL_BATCH_SIZE,
            concurrency     = EVAL_CONCURRENCY,
        )
        print(f"\nEvaluation done: {result}")
        return

    summary = run_all_scrapers()

    total_new  = sum(v["inserted"] for v in summary.values())
    total_skip = sum(v["skipped"]  for v in summary.values())
    print(f"\nScraping done. {total_new} new jobs added, {total_skip} duplicates skipped.")
    for src, counts in summary.items():
        print(
            f"  {src:<22} +{counts['inserted']} new  "
            f"{counts['skipped']} skipped  errors={counts['errors']}"
        )
    print("\nLLM evaluation running in background (check logs for progress).")


if __name__ == "__main__":
    main()
