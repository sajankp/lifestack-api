import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = structlog.get_logger(__name__)

# Single in-memory AsyncIOScheduler instance
scheduler = AsyncIOScheduler()


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
