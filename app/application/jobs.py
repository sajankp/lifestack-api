"""
app/application/jobs.py

Scheduler job wrappers (Spec 005).

Boundary rules:
  - This module owns trigger registration, workspace iteration, session
    boundaries, error isolation, and advisory-lock acquisition.
  - Business logic lives in app.application.workflows and is called from here.
  - Workflows must NOT import anything from this module.
"""

import asyncio
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select

from app.application.workflows import (
    evaluate_workspace_budget_guardrails,
    process_workspace_recurring_todos,
    process_workspace_recurring_transactions,
)
from app.core.database import postgres
from app.platform.models import Workspace

logger = structlog.get_logger(__name__)

# Postgres advisory lock key — prevents two running instances from executing
# the same job concurrently during rolling deployments.
BUDGET_GUARDRAILS_LOCK_KEY = 1001

# Maximum seconds allowed for a single workspace evaluation before it is
# abandoned. Prevents a stuck workspace from blocking scheduler shutdown.
WORKSPACE_EVALUATION_TIMEOUT_SECONDS = 300.0


async def budget_guardrails_job() -> None:
    """
    Cron-triggered job that evaluates budget guardrails across all active
    workspaces (Spec 009).

    Execution model:
      1. Acquire a Postgres advisory transaction lock to prevent concurrent
         execution during rolling deploys.
      2. Fetch the list of active workspaces.
      3. Iterate workspaces, evaluating each in its own isolated DB transaction.
         One workspace failure is logged and skipped; others continue.
      4. Each workspace evaluation has a bounded timeout to avoid unbounded
         drain on application shutdown.
    """
    start_time = datetime.now(UTC)
    logger.info("budget_guardrails_job_start", job_name="budget_guardrails_job")

    # --- Step 1: Advisory lock + workspace list fetch ---
    async with postgres.async_session_maker() as session, session.begin():
        lock_res = await session.execute(
            select(func.pg_try_advisory_xact_lock(BUDGET_GUARDRAILS_LOCK_KEY))
        )
        has_lock = lock_res.scalar()
        if not has_lock:
            logger.info(
                "budget_guardrails_job_skipped_lock_held",
                job_name="budget_guardrails_job",
            )
            return

        workspaces_res = await session.execute(
            select(Workspace).where(Workspace.is_active == True)  # noqa: E712
        )
        workspaces = workspaces_res.scalars().all()

        # --- Step 2: Per-workspace evaluation with isolated transactions ---
        for workspace in workspaces:
            ws_start = datetime.now(UTC)
            try:
                async with postgres.async_session_maker() as ws_session:  # noqa: SIM117
                    async with ws_session.begin():
                        await asyncio.wait_for(
                            evaluate_workspace_budget_guardrails(ws_session, workspace),
                            timeout=WORKSPACE_EVALUATION_TIMEOUT_SECONDS,
                        )

                duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                logger.info(
                    "budget_guardrails_workspace_success",
                    job_name="budget_guardrails_job",
                    workspace_id=workspace.id,
                    duration_ms=duration_ms,
                    status="success",
                )
            except TimeoutError:
                duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                logger.error(
                    "budget_guardrails_workspace_timeout",
                    job_name="budget_guardrails_job",
                    workspace_id=workspace.id,
                    duration_ms=duration_ms,
                    status="timeout",
                )
            except Exception:
                duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                logger.error(
                    "budget_guardrails_workspace_failed",
                    job_name="budget_guardrails_job",
                    workspace_id=workspace.id,
                    duration_ms=duration_ms,
                    status="failed",
                    exc_info=True,
                )

        total_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
        logger.info(
            "budget_guardrails_job_completed",
            job_name="budget_guardrails_job",
            duration_ms=total_ms,
            workspace_count=len(workspaces),
        )


# Postgres advisory lock key — separate from budget_guardrails (1001) to allow concurrent runs
RECURRING_TRANSACTIONS_LOCK_KEY = 1002


async def recurring_transactions_job() -> None:
    """
    Cron-triggered job that generates spending transactions for all due recurring
    rules across all active workspaces (Spec 013).

    Execution model mirrors budget_guardrails_job:
      1. Acquire a Postgres session advisory lock (key 1002) to prevent concurrent
         execution during rolling deploys.
      2. Fetch the list of active workspaces, then close the read transaction.
      3. Iterate workspaces, processing each in its own isolated DB transaction.
         One workspace failure is logged and skipped; others continue.
      4. Each workspace has a bounded timeout to avoid unbounded drain on shutdown.
    """
    start_time = datetime.now(UTC)
    logger.info("recurring_transactions_job_start", job_name="recurring_transactions_job")

    async with postgres.async_session_maker() as session:
        lock_res = await session.execute(
            select(func.pg_try_advisory_lock(RECURRING_TRANSACTIONS_LOCK_KEY))
        )
        has_lock = lock_res.scalar()
        if not has_lock:
            logger.info(
                "recurring_transactions_job_skipped_lock_held",
                job_name="recurring_transactions_job",
            )
            return

        try:
            workspaces_res = await session.execute(
                select(Workspace).where(Workspace.is_active == True)  # noqa: E712
            )
            workspaces = workspaces_res.scalars().all()
            await session.commit()

            total_generated = 0
            total_todos_generated = 0
            for workspace in workspaces:
                ws_start = datetime.now(UTC)
                try:
                    async with postgres.async_session_maker() as ws_session:  # noqa: SIM117
                        async with ws_session.begin():
                            count = await asyncio.wait_for(
                                process_workspace_recurring_transactions(ws_session, workspace),
                                timeout=WORKSPACE_EVALUATION_TIMEOUT_SECONDS,
                            )
                            total_generated += count
                            todo_count = await asyncio.wait_for(
                                process_workspace_recurring_todos(ws_session, workspace),
                                timeout=WORKSPACE_EVALUATION_TIMEOUT_SECONDS,
                            )
                            total_todos_generated += todo_count

                    duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                    logger.info(
                        "recurring_transactions_workspace_success",
                        job_name="recurring_transactions_job",
                        workspace_id=workspace.id,
                        duration_ms=duration_ms,
                        status="success",
                    )
                except TimeoutError:
                    duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                    logger.error(
                        "recurring_transactions_workspace_timeout",
                        job_name="recurring_transactions_job",
                        workspace_id=workspace.id,
                        duration_ms=duration_ms,
                        status="timeout",
                    )
                except Exception:
                    duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                    logger.error(
                        "recurring_transactions_workspace_failed",
                        job_name="recurring_transactions_job",
                        workspace_id=workspace.id,
                        duration_ms=duration_ms,
                        status="failed",
                        exc_info=True,
                    )

            total_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
            logger.info(
                "recurring_transactions_job_completed",
                job_name="recurring_transactions_job",
                duration_ms=total_ms,
                workspace_count=len(workspaces),
                total_generated=total_generated,
                total_todos_generated=total_todos_generated,
            )
        finally:
            await session.execute(select(func.pg_advisory_unlock(RECURRING_TRANSACTIONS_LOCK_KEY)))
