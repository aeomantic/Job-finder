"""
api.py – FastAPI backend.

Endpoints:
  GET  /                      → serves static/index.html
  GET  /api/jobs              → list jobs (search, source, score, limit, offset)
  GET  /api/jobs/{id}         → single job detail
  GET  /api/stats             → counts per source + evaluation summary
  GET  /api/eval-status       → quick evaluation progress snapshot
  POST /api/scrape            → trigger a scrape run in the background
  POST /api/evaluate          → trigger LLM evaluation batch in the background
  GET  /api/sources           → list distinct source values

Static files (CSS, JS) are served from the ./static directory.
"""

import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

LOG_FILE          = os.getenv("LOG_FILE",          "./logs/scraper.log")
LOG_LEVEL         = os.getenv("LOG_LEVEL",         "INFO").upper()
DB_PATH           = os.getenv("DB_PATH",           "./data/jobs.db")
HOST              = os.getenv("HOST",              "0.0.0.0")
PORT              = int(os.getenv("PORT",          "8000"))
RESUME_PDF_PATH   = os.getenv("RESUME_PDF_PATH",   "./resume.pdf")
EVAL_BATCH_SIZE   = int(os.getenv("EVAL_BATCH_SIZE",  "50"))
EVAL_CONCURRENCY  = int(os.getenv("EVAL_CONCURRENCY", "3"))

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

import database


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class StatusPayload(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db(DB_PATH)
    yield


app = FastAPI(
    title="SG Internship Aggregator",
    description=(
        "Aggregates internship postings from InternSG, MyCareersFuture, "
        "JobStreet, and Careers@Gov, with LLM-powered match scoring."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


# ---------------------------------------------------------------------------
# Job endpoints
# ---------------------------------------------------------------------------

@app.get("/api/jobs", summary="List internship jobs")
def list_jobs(
    search:     Optional[str]  = Query(None, description="Filter by title or company (case-insensitive)"),
    source:     Optional[str]  = Query(None, description="Filter by source site"),
    score:      Optional[str]  = Query(None, description="High | Medium | Low | unscored"),
    is_applied: Optional[bool] = Query(None, description="true = applied only; false = not-applied only"),
    app_status: Optional[str]  = Query(None, description="active | applied | screening | interview | offer | rejection | withdrawn | no response | unapplied"),
    limit:      int            = Query(50,   ge=1, le=200),
    offset:     int            = Query(0,    ge=0),
):
    logger.info(
        "[api] GET /api/jobs – search=%r source=%r score=%r app_status=%r limit=%d offset=%d",
        search, source, score, app_status, limit, offset,
    )
    try:
        jobs = database.get_jobs(
            DB_PATH,
            search=search, source=source, score=score,
            is_applied=is_applied, app_status=app_status,
            limit=limit, offset=offset,
        )
        logger.info("[api] /api/jobs returning %d job(s)", len(jobs))
        return {"count": len(jobs), "limit": limit, "offset": offset, "jobs": jobs}
    except Exception as exc:
        logger.error("[api] /api/jobs failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database query failed: {exc}")


@app.patch("/api/jobs/{job_id}/apply", summary="Toggle the applied state of a job")
def toggle_applied(job_id: int):
    """
    Toggles between 'applied' and 'unapplied'.
    Returns the new state including application_status and applied_at.
    """
    job = database.get_job_by_id(DB_PATH, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    current = job.get("application_status", "unapplied")
    new_status = "unapplied" if current != "unapplied" else "applied"
    result = database.update_job_status(DB_PATH, job_id, new_status)
    return result


@app.patch("/api/jobs/{job_id}/status", summary="Set the application pipeline status")
def set_status(job_id: int, payload: StatusPayload):
    """
    Set application_status to any valid pipeline state.
    Automatically stamps applied_at on the first transition away from 'unapplied'.

    Valid states: unapplied | applied | screening | interview | offer | rejection | withdrawn | no response
    """
    try:
        result = database.update_job_status(DB_PATH, job_id, payload.status)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not result:
        raise HTTPException(status_code=404, detail="Job not found")
    return result


@app.get("/api/jobs/{job_id}", summary="Get single job by ID")
def get_job(job_id: int):
    job = database.get_job_by_id(DB_PATH, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ---------------------------------------------------------------------------
# Stats & evaluation status
# ---------------------------------------------------------------------------

@app.get("/api/health", summary="Health check – verifies DB is reachable")
def health_check():
    """
    Quick liveness probe.  Opens the database, queries the row count, and
    returns it.  A 200 response with total > 0 confirms data is accessible.
    If the DB is unreachable, this returns a 500 with the error message.
    """
    try:
        stats = database.get_stats(DB_PATH)
        return {"status": "ok", "db_path": DB_PATH, "total_jobs": stats["total"]}
    except Exception as exc:
        logger.error("[api] /api/health DB check failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"DB unreachable: {exc}")


@app.get("/api/stats", summary="Aggregation and evaluation statistics")
def get_stats():
    return database.get_stats(DB_PATH)


@app.get("/api/eval-status", summary="Quick LLM evaluation progress snapshot")
def get_eval_status():
    """
    Returns how many jobs have been scored, broken down by score tier,
    plus whether an evaluation batch is currently running.
    """
    from evaluator import is_eval_running
    stats = database.get_stats(DB_PATH)
    scored = sum(stats.get("eval_scores", {}).values())
    return {
        "total":      stats["total"],
        "scored":     scored,
        "unscored":   stats["unscored"],
        "by_score":   stats.get("eval_scores", {}),
        "is_running": is_eval_running(),
    }


@app.get("/api/sources", summary="List distinct source names")
def get_sources():
    stats = database.get_stats(DB_PATH)
    return list(stats.get("by_source", {}).keys())


# ---------------------------------------------------------------------------
# Scrape trigger
# ---------------------------------------------------------------------------

_scrape_running = False


def _run_scrape_sync() -> None:
    global _scrape_running
    if _scrape_running:
        logger.warning("Scrape already running – skipping new request")
        return
    _scrape_running = True
    try:
        from orchestrator import run_all_scrapers
        run_all_scrapers()   # also fires _launch_eval_background() internally
    finally:
        _scrape_running = False


@app.post("/api/scrape", summary="Trigger a full scrape run (background)")
def trigger_scrape(background_tasks: BackgroundTasks):
    if _scrape_running:
        return JSONResponse(status_code=202, content={"message": "Scrape already in progress."})
    background_tasks.add_task(_run_scrape_sync)
    return {"message": "Scrape started in background. LLM evaluation will follow automatically."}


# ---------------------------------------------------------------------------
# Evaluation trigger
# ---------------------------------------------------------------------------

@app.post("/api/evaluate", summary="Trigger LLM evaluation batch (background)")
async def trigger_evaluate(background_tasks: BackgroundTasks):
    """
    Manually kick off an evaluation pass on all currently unscored jobs.
    Safe to call while a scrape is running – uses the shared lock in evaluator.py.
    """
    from evaluator import is_eval_running, run_evaluation_batch_guarded

    if is_eval_running():
        return JSONResponse(
            status_code=202,
            content={"message": "LLM evaluation already in progress."},
        )

    async def _eval_task() -> None:
        await run_evaluation_batch_guarded(
            db_path         = DB_PATH,
            resume_pdf_path = RESUME_PDF_PATH,
            batch_size      = EVAL_BATCH_SIZE,
            concurrency     = EVAL_CONCURRENCY,
        )

    background_tasks.add_task(_eval_task)
    return {
        "message": (
            f"LLM evaluation started in background "
            f"(batch_size={EVAL_BATCH_SIZE}, concurrency={EVAL_CONCURRENCY}). "
            "Poll /api/eval-status for progress."
        )
    }


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def serve_index():
    index = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index):
        raise HTTPException(status_code=404, detail="Frontend not found")
    return FileResponse(index)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host=HOST, port=PORT, reload=False)
