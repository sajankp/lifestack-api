import random

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings

logger = structlog.get_logger(__name__)

# Single in-memory AsyncIOScheduler instance
scheduler = AsyncIOScheduler()

# Jitter range for daily jobs (in minutes) - spreads jobs across the day
# Jobs that were clustered at 02:00-07:00 will now be spread 00:00-23:59
DAILY_JOB_JITTER_MINUTES = 60  # ±60 minutes (2 hour window)


def _jittered_time(
    hour_utc: int, minute_utc: int = 0, jitter_minutes: int = DAILY_JOB_JITTER_MINUTES
) -> tuple[int, int]:
    """Add random jitter to a scheduled time, wrapping around midnight if needed.

    Returns (hour, minute) tuple with jitter applied.
    """
    total_minutes = hour_utc * 60 + minute_utc
    jitter = random.randint(-jitter_minutes, jitter_minutes)
    total_minutes = (total_minutes + jitter) % (24 * 60)
    return total_minutes // 60, total_minutes % 60


def register_interval_job(
    job_func,
    *,
    job_id: str,
    hours: int = 0,
    minutes: int = 0,
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
        minutes=minutes,
        id=job_id,
        replace_existing=True,
    )


def register_daily_job(
    job_func,
    *,
    job_id: str,
    hour_utc: int,
    minute_utc: int = 0,
    use_jitter: bool = False,
    jitter_minutes: int = DAILY_JOB_JITTER_MINUTES,
) -> None:
    """Register a daily cron job anchored to a specific UTC hour/minute.

    Args:
        job_func: The async function to run
        job_id: Unique identifier for the job
        hour_utc: Base hour in UTC (0-23)
        minute_utc: Base minute in UTC (0-59)
        use_jitter: Whether to apply random jitter (default: False — existing
            callers rely on a fixed time, e.g. morning_briefing_job running
            after the weekly_summary cron; opt in explicitly via
            ``register_daily_job_staggered`` instead of flipping this default)
        jitter_minutes: Maximum minutes of jitter in either direction (default: 60)
    """
    if use_jitter:
        jittered_hour, jittered_minute = _jittered_time(hour_utc, minute_utc, jitter_minutes)
        logger.info(
            "scheduler_daily_job_jittered",
            job_id=job_id,
            original_hour=hour_utc,
            original_minute=minute_utc,
            jittered_hour=jittered_hour,
            jittered_minute=jittered_minute,
        )
    else:
        jittered_hour, jittered_minute = hour_utc, minute_utc

    scheduler.add_job(
        job_func,
        "cron",
        hour=jittered_hour,
        minute=jittered_minute,
        id=job_id,
        replace_existing=True,
        timezone="UTC",
    )


def register_daily_job_staggered(
    job_func,
    *,
    job_id: str,
    hour_utc: int,
    minute_utc: int = 0,
    jitter_minutes: int = DAILY_JOB_JITTER_MINUTES,
) -> None:
    """Register a daily cron job with jitter to spread load across the day.

    Thin wrapper around ``register_daily_job`` with jitter explicitly enabled.
    Use this for jobs that were previously clustered in the early morning hours
    to distribute them across 00:00-23:59 UTC.
    """
    register_daily_job(
        job_func,
        job_id=job_id,
        hour_utc=hour_utc,
        minute_utc=minute_utc,
        use_jitter=True,
        jitter_minutes=jitter_minutes,
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
