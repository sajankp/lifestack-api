# Spec 005: Scheduler and Background Jobs

**Status:** Planned
**Spec ID:** 005

## Problem Statement
Various background tasks (like checking a budget, sending reminders, or processing recurring transactions) need to execute outside the main HTTP request lifecycle. A simple, reliable, in-process scheduler is needed for Stage 1 of the Lifestack architecture before more complex distributed message queues (like Celery/Redis PubSub) are necessary.

## Proposed Solution
Leverage `APScheduler` (specifically `AsyncIOScheduler`) embedded within the FastAPI application lifecycle to manage time-based workflows. Workflows should be modeled in `app/application/workflows.py` using direct module service injection, and executed from cron-like jobs defined in `app/application/jobs.py`.

Version policy for this slice:
- Pin APScheduler to `>=3.11,<4` to avoid v4 API transition during stage-1 delivery.

## Data Model Changes
None. (We are not using a DB-backed outbox or job status table yet; standard in-process memory constraints apply for Stage 1).

## API Changes
No new REST endpoints required.

## Stage 1 Deployment Decision
Stage 1 runs scheduler jobs from exactly one process instance (single scheduler leader) to avoid duplicate execution across ASGI workers.

Implementation constraints for this iteration:
- `APScheduler` runs only when `SCHEDULER_ENABLED=true`.
- Non-leader API instances must start without scheduler jobs.
- Production deploy config must ensure only one instance has `SCHEDULER_ENABLED=true`.
- If multi-instance scheduling is required later, we will adopt a persistent shared job store and leader/locking strategy in a follow-up spec.

Boundary rule:
- `app/application/jobs.py` owns scheduler wrappers (trigger registration, workspace iteration, session and error boundaries).
- `app/application/workflows.py` owns business logic.
- Jobs call workflows; workflows do not manage scheduler concerns.

## Implementation Plan
1. Add `apscheduler` to dependencies (`pyproject.toml` or `requirements.txt`/`uv`).
2. Create `app/core/scheduler.py` that configures an `AsyncIOScheduler`.
3. Create `app/application/jobs.py` to define top-level run boundaries, ensuring that each job instantiates its own DB `Session` and handles workspace batching correctly (so one workspace's failure doesn't poison the job batch).
4. Integrate scheduler start/stop events into FastAPI's `lifespan` context manager in `app/main.py`, gated by `SCHEDULER_ENABLED`.
5. On shutdown, stop scheduler with bounded waiting and ensure in-progress jobs have max runtime protections.

## Test Strategy
- **Unit Tests:** Mock the `AsyncIOScheduler` and assert that the correct jobs are added with proper intervals.
- **Integration Tests:** Verify that a workflow function correctly checks out a session, instantiates module services, runs its domain logic, and commits cleanly.

## Acceptance Criteria
- `apscheduler` dependency is present and wired through `app/core/scheduler.py`.
- Scheduler starts only when `SCHEDULER_ENABLED=true`; otherwise no jobs are registered.
- Each job run creates its own DB session boundary and isolates workspace failures.
- Lifespan shutdown cleanly stops the scheduler.
- At least one automated test validates gating behavior and one validates job registration.
- Job wrappers enforce execution timeout/guardrails to avoid unbounded deploy drain on shutdown.
- Scheduler job run metrics/logs are emitted for success/failure visibility.

## Observability Hooks
- Emit structured logs for job start/end with `job_name`, `workspace_id` (when applicable), `duration_ms`, `status`.
- Emit counters for scheduler job successes/failures and histogram for runtime duration.
- Emit trace spans around job wrapper and workflow execution segments.
