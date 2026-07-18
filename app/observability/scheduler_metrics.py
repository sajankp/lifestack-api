"""Prometheus metrics for scheduler job observability."""

import time
from contextlib import asynccontextmanager

from prometheus_client import Counter, Gauge, Histogram

# Job execution counters
job_started_total = Counter(
    "lifestack_scheduler_job_started_total",
    "Total number of scheduler job executions started",
    ["job_name"],
)

job_succeeded_total = Counter(
    "lifestack_scheduler_job_succeeded_total",
    "Total number of scheduler job executions that succeeded",
    ["job_name"],
)

job_failed_total = Counter(
    "lifestack_scheduler_job_failed_total",
    "Total number of scheduler job executions that failed",
    ["job_name"],
)

job_duration_seconds = Histogram(
    "lifestack_scheduler_job_duration_seconds",
    "Duration of scheduler job executions in seconds",
    ["job_name"],
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0],
)

# Job status gauge (1 = running, 0 = not running)
job_running = Gauge(
    "lifestack_scheduler_job_running",
    "Whether a scheduler job is currently running (1) or not (0)",
    ["job_name"],
)

# Workspace-level job metrics
workspace_job_started_total = Counter(
    "lifestack_scheduler_workspace_job_started_total",
    "Total number of workspace-level job executions started",
    ["job_name", "workspace_id"],
)

workspace_job_succeeded_total = Counter(
    "lifestack_scheduler_workspace_job_succeeded_total",
    "Total number of workspace-level job executions that succeeded",
    ["job_name", "workspace_id"],
)

workspace_job_failed_total = Counter(
    "lifestack_scheduler_workspace_job_failed_total",
    "Total number of workspace-level job executions that failed",
    ["job_name", "workspace_id"],
)

workspace_job_duration_seconds = Histogram(
    "lifestack_scheduler_workspace_job_duration_seconds",
    "Duration of workspace-level scheduler job executions in seconds",
    ["job_name", "workspace_id"],
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
)

# Scheduler health
scheduler_running = Gauge(
    "lifestack_scheduler_running",
    "Whether the scheduler is running (1) or stopped (0)",
)

jobs_registered = Gauge(
    "lifestack_scheduler_jobs_registered",
    "Number of jobs currently registered in the scheduler",
)


def record_job_start(job_name: str) -> None:
    """Record that a job has started."""
    job_started_total.labels(job_name=job_name).inc()
    job_running.labels(job_name=job_name).set(1)


def record_job_success(job_name: str, duration_seconds: float) -> None:
    """Record that a job succeeded."""
    job_succeeded_total.labels(job_name=job_name).inc()
    job_duration_seconds.labels(job_name=job_name).observe(duration_seconds)
    job_running.labels(job_name=job_name).set(0)


def record_job_failure(job_name: str, duration_seconds: float) -> None:
    """Record that a job failed."""
    job_failed_total.labels(job_name=job_name).inc()
    job_duration_seconds.labels(job_name=job_name).observe(duration_seconds)
    job_running.labels(job_name=job_name).set(0)


def record_workspace_job_start(job_name: str, workspace_id: int) -> None:
    """Record that a workspace-level job has started."""
    workspace_job_started_total.labels(job_name=job_name, workspace_id=str(workspace_id)).inc()


def record_workspace_job_success(job_name: str, workspace_id: int, duration_seconds: float) -> None:
    """Record that a workspace-level job succeeded."""
    workspace_job_succeeded_total.labels(job_name=job_name, workspace_id=str(workspace_id)).inc()
    workspace_job_duration_seconds.labels(
        job_name=job_name, workspace_id=str(workspace_id)
    ).observe(duration_seconds)


def record_workspace_job_failure(job_name: str, workspace_id: int, duration_seconds: float) -> None:
    """Record that a workspace-level job failed."""
    workspace_job_failed_total.labels(job_name=job_name, workspace_id=str(workspace_id)).inc()
    workspace_job_duration_seconds.labels(
        job_name=job_name, workspace_id=str(workspace_id)
    ).observe(duration_seconds)


def set_scheduler_running(is_running: bool) -> None:
    """Set the scheduler running status."""
    scheduler_running.set(1 if is_running else 0)


def set_jobs_registered(count: int) -> None:
    """Set the number of registered jobs."""
    jobs_registered.set(count)


@asynccontextmanager
async def track_job(job_name: str, workspace_id: int | None = None):
    """Context manager to automatically track job execution metrics.

    Usage:
        async with track_job("my_job", workspace_id=123):
            await do_work()
    """
    start_time = time.monotonic()
    record_job_start(job_name)
    if workspace_id is not None:
        record_workspace_job_start(job_name, workspace_id)

    try:
        yield
        duration = time.monotonic() - start_time
        record_job_success(job_name, duration)
        if workspace_id is not None:
            record_workspace_job_success(job_name, workspace_id, duration)
    except Exception:
        duration = time.monotonic() - start_time
        record_job_failure(job_name, duration)
        if workspace_id is not None:
            record_workspace_job_failure(job_name, workspace_id, duration)
        raise
