import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings

logger = structlog.get_logger(__name__)

# Single in-memory AsyncIOScheduler instance
scheduler = AsyncIOScheduler()


def register_interval_job(
    job_func,
    *,
    job_id: str,
    hours: int,
    idempotent: bool = True,
) -> None:
    """Register an interval job with safety checks for non-idempotent work."""
    if not idempotent and not settings.SCHEDULER_ALLOW_NON_IDEMPOTENT_JOBS:
        logger.critical(
            "scheduler_non_idempotent_job_blocked",
            job_id=job_id,
            setting="SCHEDULER_ALLOW_NON_IDEMPOTENT_JOBS",
            reason=(
                "Non-idempotent scheduled jobs are disabled by default to avoid "
                "duplicate side effects during rolling deploy windows."
            ),
        )
        raise RuntimeError(
            "Refusing to register non-idempotent scheduler job while "
            "SCHEDULER_ALLOW_NON_IDEMPOTENT_JOBS is false."
        )

    scheduler.add_job(
        job_func,
        "interval",
        hours=hours,
        id=job_id,
        replace_existing=True,
    )


def register_daily_job(
    job_func,
    *,
    job_id: str,
    hour_utc: int,
    minute_utc: int = 0,
) -> None:
    """Register a daily cron job anchored to a specific UTC hour/minute."""
    scheduler.add_job(
        job_func,
        "cron",
        hour=hour_utc,
        minute=minute_utc,
        id=job_id,
        replace_existing=True,
        timezone="UTC",
    )


def start_scheduler() -> None:
    """Start the background scheduler."""
    if not scheduler.running:
        logger.info("scheduler_starting")
        scheduler.start()
    else:
        logger.warning("scheduler_already_running")


def shutdown_scheduler() -> None:
    """Shut down the background scheduler, waiting for active jobs to finish."""
    if scheduler.running:
        logger.info("scheduler_shutting_down")
        scheduler.shutdown(wait=True)
    else:
        logger.warning("scheduler_not_running")
