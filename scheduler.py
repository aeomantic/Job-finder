"""
scheduler.py – APScheduler wrapper.

Runs the full scraper pipeline on first start and then every N hours.
Called from api.py lifespan context so it shares the same process.
"""

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _scrape_job(db_path: str):
    """APScheduler job function – runs the full orchestrator pipeline."""
    logger.info("[scheduler] Starting scheduled scrape run")
    try:
        # Import here to avoid circular imports at module load time
        from orchestrator import run_all_scrapers
        run_all_scrapers()
    except Exception as exc:
        logger.error("[scheduler] Scheduled scrape failed: %s", exc, exc_info=True)


def start_scheduler(db_path: str, interval_hours: float = 6.0):
    """
    Start the background scheduler.

    - Runs one scrape immediately on startup.
    - Then repeats every `interval_hours` hours.
    """
    global _scheduler

    if _scheduler and _scheduler.running:
        logger.warning("[scheduler] Already running – not starting again")
        return

    _scheduler = BackgroundScheduler(timezone="Asia/Singapore")

    _scheduler.add_job(
        _scrape_job,
        trigger=IntervalTrigger(hours=interval_hours),
        args=[db_path],
        id="full_scrape",
        name="Full internship scrape",
        replace_existing=True,
        # Run once immediately when the scheduler starts
        next_run_time=None,   # will be set via misfire_grace_time on first add
    )

    _scheduler.start()
    logger.info(
        "[scheduler] Started – will scrape every %.1f hours (Asia/Singapore time)",
        interval_hours,
    )

    # Trigger immediately in a separate thread so the API server starts fast
    import threading

    def _immediate():
        logger.info("[scheduler] Running immediate startup scrape…")
        _scrape_job(db_path)

    t = threading.Thread(target=_immediate, daemon=True)
    t.start()


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[scheduler] Stopped")
