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
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.insights import generate_workspace_insights
from app.application.workflows import (
    MorningBriefingWorkflow,
    cleanup_expired_exports,
    cleanup_expired_sessions,
    cleanup_import_previews,
    deliver_pending_push_notifications,
    evaluate_workspace_budget_guardrails,
    evaluate_workspace_kpi_breaches,
    ingest_fx_rates,
    process_workspace_medication_reminders,
    process_workspace_recurring_todos,
    process_workspace_recurring_transactions,
    process_workspace_todo_reminders,
)
from app.config import settings
from app.core.constants import (
    ADVISORY_LOCK_BHAVCOPY_PRICE_FEED,
    ADVISORY_LOCK_BUDGET_GUARDRAILS,
    ADVISORY_LOCK_DASHBOARD_INSIGHTS,
    ADVISORY_LOCK_EXPORT_CLEANUP,
    ADVISORY_LOCK_FX_RATE_INGESTION,
    ADVISORY_LOCK_IMPORT_PREVIEW_CLEANUP,
    ADVISORY_LOCK_INVESTMENT_CLOSING_PRICES,
    ADVISORY_LOCK_KPI_GUARDRAILS,
    ADVISORY_LOCK_MEDICATION_REMINDER,
    ADVISORY_LOCK_MORNING_BRIEFING,
    ADVISORY_LOCK_NET_WORTH_SNAPSHOT,
    ADVISORY_LOCK_PUSH_DELIVERY,
    ADVISORY_LOCK_RECURRING_TRANSACTIONS,
    ADVISORY_LOCK_SESSION_CLEANUP,
    ADVISORY_LOCK_TODO_REMINDER,
    ADVISORY_LOCK_WEEKLY_SUMMARY,
)
from app.core.database import postgres
from app.finance.repository import AccountRepository, FinanceSettingRepository, FxRateRepository
from app.health.repository import (
    MedicationEventRepository,
    MedicationRepository,
    WeightEntryRepository,
)
from app.health.service import HealthService
from app.imports.repository import ImportRepository
from app.investing import service as investing_service
from app.investing.models import InstrumentType
from app.investing.performance_service import PerformanceService
from app.investing.repository import (
    CashBalanceRepository,
    HoldingPriceRepository,
    HoldingRepository,
    InstrumentRepository,
    PortfolioSnapshotRepository,
)
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService
from app.platform.models import Workspace, WorkspaceMembership, WorkspaceRole
from app.spending.repository import (
    BudgetRepository,
    CategoryGroupRepository,
    CategoryRepository,
    RecurringTransactionRepository,
    TransactionRepository,
)
from app.spending.service import BudgetService, RecurringTransactionService
from app.summaries.repository import WeeklySummaryRepository
from app.summaries.service import WeeklySummaryService
from app.todo.repository import TodoRepository
from app.todo.service import TodoService

logger = structlog.get_logger(__name__)
# ... (rest unchanged)

# Advisory lock key — see app.core.constants for the full registry
BUDGET_GUARDRAILS_LOCK_KEY = ADVISORY_LOCK_BUDGET_GUARDRAILS

# Maximum seconds allowed for a single workspace evaluation before it is
# abandoned. Prevents a stuck workspace from blocking scheduler shutdown.
WORKSPACE_EVALUATION_TIMEOUT_SECONDS = 300.0


async def run_workspace_job(
    *,
    job_name: str,
    lock_key: int,
    process_workspace: Callable[[AsyncSession, Workspace], Awaitable[None]],
    timeout_seconds: float = WORKSPACE_EVALUATION_TIMEOUT_SECONDS,
    workspace_id: int | None = None,
) -> None:
    """Run ``process_workspace`` for each active workspace under an advisory
    lock, isolating failures per workspace.

    Holds exactly ONE pooled connection for the whole run: a *session-level*
    advisory lock (``pg_try_advisory_lock``) is taken on the same session that
    does the per-workspace work, and each workspace runs in its own
    timeout-bounded transaction on that session (session-level locks survive
    ``COMMIT``, unlike ``pg_try_advisory_xact_lock``). The previous shape —
    an outer lock-holder session kept open while a second per-workspace
    session was checked out inside the loop — could deadlock a constrained
    pool under concurrent job runs (see PR #104 review): lock-holder
    connections exhaust the pool, then every inner checkout waits on a
    connection no blocked outer holder will release.

    One workspace's failure is rolled back, logged, and skipped; others
    continue. A workspace *timeout* cancels a query mid-flight, which may
    invalidate the shared connection — remaining workspaces then fail fast
    and the server releases the advisory lock on disconnect, so the next
    scheduled run recovers. Log event names (``{job_name}_start``,
    ``_skipped_lock_held``, ``_workspace_not_found_or_inactive``,
    ``_workspace_success``, ``_workspace_timeout``, ``_workspace_failed``,
    ``_completed``) match what the jobs logged before extraction — do not
    rename them without checking ``test_scheduler.py`` and any log-based
    alerting.
    """
    start_time = datetime.now(UTC)
    logger.info(f"{job_name}_start", job_name=job_name)

    async with postgres.async_session_maker() as session:
        lock_res = await session.execute(select(func.pg_try_advisory_lock(lock_key)))
        has_lock = lock_res.scalar()
        if not has_lock:
            await session.rollback()
            logger.info(f"{job_name}_skipped_lock_held", job_name=job_name)
            return

        try:
            # Fetch plain ids, not ORM objects: a later per-workspace rollback
            # expires every object loaded on this session, and touching an
            # expired attribute afterwards would raise (async lazy-load).
            if workspace_id is not None:
                ids_res = await session.execute(
                    select(Workspace.id).where(Workspace.id == workspace_id, Workspace.is_active)
                )
            else:
                ids_res = await session.execute(select(Workspace.id).where(Workspace.is_active))
            workspace_ids = list(ids_res.scalars().all())
            if workspace_id is not None and not workspace_ids:
                logger.warning(
                    f"{job_name}_workspace_not_found_or_inactive",
                    workspace_id=workspace_id,
                )
            # End the read transaction; the session-level advisory lock survives.
            await session.commit()

            for ws_id in workspace_ids:
                ws_start = datetime.now(UTC)
                try:
                    async with session.begin():
                        workspace = await session.get(Workspace, ws_id)
                        if workspace is None or not workspace.is_active:
                            continue
                        await asyncio.wait_for(
                            process_workspace(session, workspace),
                            timeout=timeout_seconds,
                        )

                    duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                    logger.info(
                        f"{job_name}_workspace_success",
                        job_name=job_name,
                        workspace_id=ws_id,
                        duration_ms=duration_ms,
                        status="success",
                    )
                except TimeoutError:
                    duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                    logger.error(
                        f"{job_name}_workspace_timeout",
                        job_name=job_name,
                        workspace_id=ws_id,
                        duration_ms=duration_ms,
                        status="timeout",
                    )
                except Exception:
                    duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                    logger.error(
                        f"{job_name}_workspace_failed",
                        job_name=job_name,
                        workspace_id=ws_id,
                        duration_ms=duration_ms,
                        status="failed",
                        exc_info=True,
                    )

            total_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
            logger.info(
                f"{job_name}_completed",
                job_name=job_name,
                duration_ms=total_ms,
                workspace_count=len(workspace_ids),
            )
        finally:
            await _release_session_advisory_lock(session, lock_key, job_name)


async def _release_session_advisory_lock(
    session: AsyncSession, lock_key: int, job_name: str
) -> None:
    """Release a session-level advisory lock, tolerating a dead connection.

    If a cancelled query invalidated the connection, the unlock fails here but
    Postgres already released the lock server-side when the connection dropped.
    """
    try:
        await session.execute(select(func.pg_advisory_unlock(lock_key)))
        await session.commit()
    except Exception:
        logger.warning(f"{job_name}_lock_release_failed", job_name=job_name, exc_info=True)


# Advisory lock key — see app.core.constants for the full registry
INVESTMENT_CLOSING_PRICES_LOCK_KEY = ADVISORY_LOCK_INVESTMENT_CLOSING_PRICES


async def investment_closing_prices_job(workspace_id: int | None = None) -> None:
    """Cache each workspace's latest completed market close once per day.

    Brought under run_workspace_job's session-level advisory lock (was
    previously the only per-workspace job managing its own per-workspace
    sessions with no lock at all — two concurrent instances during a rolling
    deploy could double-write holding_prices for the same close date).
    """

    async def _process_workspace(session: AsyncSession, workspace: Workspace) -> None:
        service = PerformanceService(
            HoldingRepository(session),
            CashBalanceRepository(session),
            HoldingPriceRepository(session),
            PortfolioSnapshotRepository(session),
            FinanceSettingRepository(session),
            FxRateRepository(session),
            InstrumentRepository(session),
        )
        updated = await service.refresh_workspace_prices(workspace.id)
        logger.info(
            "investment_closing_prices_workspace_completed",
            workspace_id=workspace.id,
            updated_symbols=sorted(updated),
        )

    await run_workspace_job(
        job_name="investment_closing_prices_job",
        lock_key=INVESTMENT_CLOSING_PRICES_LOCK_KEY,
        process_workspace=_process_workspace,
        workspace_id=workspace_id,
    )


# Advisory lock key — see app.core.constants for the full registry
BHAVCOPY_PRICE_FEED_LOCK_KEY = ADVISORY_LOCK_BHAVCOPY_PRICE_FEED


async def bhavcopy_price_feed_job() -> None:
    """Pre-fill HoldingPrice from NSE's official bhavcopy for INR stock
    holdings (spec-057), before investment_closing_prices_job's Yahoo-backed
    refresh runs. That job already skips holdings priced for the expected
    close date, so a bhavcopy hit here means it never falls through to
    Yahoo for that symbol; a bhavcopy miss (feed outage, delisted, BSE-only)
    is unaffected and still gets priced by the existing Yahoo path.
    """
    trade_date = investing_service._previous_weekday(datetime.now(UTC).date())

    async with httpx.AsyncClient(timeout=15.0) as client:
        bhavcopy = await investing_service._fetch_nse_bhavcopy(client, trade_date)

    if not bhavcopy:
        logger.info("bhavcopy_price_feed_no_data", trade_date=trade_date.isoformat())
        return

    async def _process_workspace(session: AsyncSession, workspace: Workspace) -> None:
        holding_repo = HoldingRepository(session)
        holdings, _ = await holding_repo.get_all(workspace.id, limit=10000, offset=0)
        if not holdings:
            return

        instrument_repo = InstrumentRepository(session)
        instruments = await instrument_repo.get_by_ids([
            h.instrument_id for h in holdings if h.instrument_id
        ])

        to_price: list[tuple[int, Decimal]] = []
        for holding in holdings:
            if holding.id is None or holding.currency.upper().strip() != "INR":
                continue
            instrument = instruments.get(holding.instrument_id)
            kind = instrument.instrument_type if instrument else InstrumentType.stock.value
            if kind == InstrumentType.mutual_fund.value:
                continue
            match = bhavcopy.get(holding.symbol.upper().strip())
            if match is None:
                continue
            _, close = match
            to_price.append((holding.id, close))

        if to_price:
            price_repo = HoldingPriceRepository(session)
            await price_repo.bulk_upsert_prices(
                workspace.id, trade_date, to_price, source="bhavcopy"
            )

    await run_workspace_job(
        job_name="bhavcopy_price_feed_job",
        lock_key=BHAVCOPY_PRICE_FEED_LOCK_KEY,
        process_workspace=_process_workspace,
    )


async def budget_guardrails_job(workspace_id: int | None = None) -> None:
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
    await run_workspace_job(
        job_name="budget_guardrails_job",
        lock_key=BUDGET_GUARDRAILS_LOCK_KEY,
        workspace_id=workspace_id,
        process_workspace=lambda s, ws: evaluate_workspace_budget_guardrails(s, ws),
    )


# Advisory lock key — separate from budget_guardrails to allow concurrent runs
KPI_GUARDRAILS_LOCK_KEY = ADVISORY_LOCK_KPI_GUARDRAILS


async def kpi_guardrails_job(workspace_id: int | None = None) -> None:
    """Cron-triggered job that evaluates custom financial KPI targets across
    all active workspaces (spec-077), writing `Notification` rows
    (category="kpi") directly on breach — rides the same cadence as
    budget_guardrails_job but its own advisory lock key so the two can run
    concurrently."""
    await run_workspace_job(
        job_name="kpi_guardrails_job",
        lock_key=KPI_GUARDRAILS_LOCK_KEY,
        workspace_id=workspace_id,
        process_workspace=lambda s, ws: evaluate_workspace_kpi_breaches(s, ws),
    )


# Advisory lock key — see app.core.constants for the full registry
DASHBOARD_INSIGHTS_LOCK_KEY = ADVISORY_LOCK_DASHBOARD_INSIGHTS


async def dashboard_insights_job(workspace_id: int | None = None) -> None:
    """Cron-triggered job that runs the three dashboard-insight detectors
    (spending anomaly, budget pace, new recurring charge — spec-058) across
    all active workspaces, writing `Notification` rows (category="insight")."""
    await run_workspace_job(
        job_name="dashboard_insights_job",
        lock_key=DASHBOARD_INSIGHTS_LOCK_KEY,
        workspace_id=workspace_id,
        process_workspace=lambda s, ws: generate_workspace_insights(s, ws),
    )


# Advisory lock key — separate from budget_guardrails to allow concurrent runs
RECURRING_TRANSACTIONS_LOCK_KEY = ADVISORY_LOCK_RECURRING_TRANSACTIONS


async def recurring_transactions_job(workspace_id: int | None = None) -> None:
    """
    Cron-triggered job that generates spending transactions for all due recurring
    rules across all active workspaces (Spec 013).

    Execution model mirrors run_workspace_job's single-connection shape:
      1. Acquire a Postgres session-level advisory lock (key 1002) on the ONE
         session the whole job uses, to prevent concurrent execution during
         rolling deploys (a second connection alongside the lock holder risks
         pool-deadlock under load — see PR #104 review).
      2. Fetch the list of active workspaces, then commit the read transaction
         (the session-level lock survives COMMIT).
      3. Iterate workspaces, processing each in its own isolated transaction
         on that same session. One workspace failure is rolled back, logged,
         and skipped; others continue.
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
            await session.rollback()
            logger.info(
                "recurring_transactions_job_skipped_lock_held",
                job_name="recurring_transactions_job",
            )
            return

        try:
            if workspace_id is not None:
                workspaces_res = await session.execute(
                    select(Workspace.id).where(Workspace.id == workspace_id, Workspace.is_active)
                )
            else:
                workspaces_res = await session.execute(
                    select(Workspace.id).where(Workspace.is_active)
                )
            workspace_ids = list(workspaces_res.scalars().all())
            if workspace_id is not None and not workspace_ids:
                logger.warning(
                    "recurring_transactions_job_workspace_not_found_or_inactive",
                    workspace_id=workspace_id,
                )
            await session.commit()

            total_generated = 0
            total_todos_generated = 0
            for ws_id in workspace_ids:
                ws_start = datetime.now(UTC)
                try:
                    async with session.begin():
                        workspace = await session.get(Workspace, ws_id)
                        if workspace is None or not workspace.is_active:
                            continue
                        count = await asyncio.wait_for(
                            process_workspace_recurring_transactions(session, workspace),
                            timeout=WORKSPACE_EVALUATION_TIMEOUT_SECONDS,
                        )
                        total_generated += count
                        todo_count = await asyncio.wait_for(
                            process_workspace_recurring_todos(session, workspace),
                            timeout=WORKSPACE_EVALUATION_TIMEOUT_SECONDS,
                        )
                        total_todos_generated += todo_count

                    duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                    logger.info(
                        "recurring_transactions_workspace_success",
                        job_name="recurring_transactions_job",
                        workspace_id=ws_id,
                        duration_ms=duration_ms,
                        status="success",
                    )
                except TimeoutError:
                    duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                    logger.error(
                        "recurring_transactions_workspace_timeout",
                        job_name="recurring_transactions_job",
                        workspace_id=ws_id,
                        duration_ms=duration_ms,
                        status="timeout",
                    )
                except Exception:
                    duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                    logger.error(
                        "recurring_transactions_workspace_failed",
                        job_name="recurring_transactions_job",
                        workspace_id=ws_id,
                        duration_ms=duration_ms,
                        status="failed",
                        exc_info=True,
                    )

            total_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
            logger.info(
                "recurring_transactions_job_completed",
                job_name="recurring_transactions_job",
                duration_ms=total_ms,
                workspace_count=len(workspace_ids),
                total_generated=total_generated,
                total_todos_generated=total_todos_generated,
            )
        finally:
            await _release_session_advisory_lock(
                session, RECURRING_TRANSACTIONS_LOCK_KEY, "recurring_transactions_job"
            )


WEEKLY_SUMMARY_LOCK_KEY = ADVISORY_LOCK_WEEKLY_SUMMARY

WEEKLY_SUMMARY_ROLE_ORDER = {
    WorkspaceRole.OWNER: 0,
    WorkspaceRole.ADMIN: 1,
    WorkspaceRole.MEMBER: 2,
    WorkspaceRole.VIEWER: 3,
}


async def weekly_summary_job(
    workspace_id: int | None = None, week_start: date | None = None
) -> None:
    """
    Cron-triggered job that generates weekly summaries across all active workspaces.
    Runs every Monday at 01:30 UTC.
    """
    start_time = datetime.now(UTC)
    logger.info("weekly_summary_job_start", job_name="weekly_summary_job")

    async with postgres.async_session_maker() as session:
        # Session-level advisory lock on the SAME session that does the work —
        # one pooled connection for the whole run (see run_workspace_job).
        lock_res = await session.execute(select(func.pg_try_advisory_lock(WEEKLY_SUMMARY_LOCK_KEY)))
        has_lock = lock_res.scalar()
        if not has_lock:
            await session.rollback()
            logger.info(
                "weekly_summary_job_skipped_lock_held",
                job_name="weekly_summary_job",
            )
            return

        try:
            # Fetch plain rows, not ORM objects: a later per-workspace rollback
            # expires every object loaded on this session, and touching an
            # expired attribute afterwards would raise (async lazy-load).
            if workspace_id is not None:
                ids_res = await session.execute(
                    select(Workspace.id).where(Workspace.id == workspace_id, Workspace.is_active)
                )
            else:
                ids_res = await session.execute(select(Workspace.id).where(Workspace.is_active))
            workspace_ids = list(ids_res.scalars().all())
            if workspace_id is not None and not workspace_ids:
                logger.warning(
                    "weekly_summary_job_workspace_not_found_or_inactive",
                    workspace_id=workspace_id,
                )
            # user ids per workspace, sorted owner → admin → member → viewer
            user_ids_by_workspace: dict[int, list[int]] = {}
            if workspace_ids:
                memberships_res = await session.execute(
                    select(
                        WorkspaceMembership.workspace_id,
                        WorkspaceMembership.user_id,
                        WorkspaceMembership.role,
                    ).where(WorkspaceMembership.workspace_id.in_(workspace_ids))
                )
                rows_by_workspace: dict[int, list[tuple[int, object]]] = {}
                for ws_id, user_id, role in memberships_res.all():
                    rows_by_workspace.setdefault(ws_id, []).append((user_id, role))
                for ws_id, rows in rows_by_workspace.items():
                    rows.sort(key=lambda row: WEEKLY_SUMMARY_ROLE_ORDER.get(row[1], 99))
                    user_ids_by_workspace[ws_id] = [user_id for user_id, _role in rows]
            # End the read transaction; the session-level advisory lock survives.
            await session.commit()

            # Calculate week_start as the Monday of the previous week
            if week_start is None:
                today = start_time.date()
                days_since_monday = today.weekday()
                last_monday = today - timedelta(days=days_since_monday + 7)
            else:
                last_monday = week_start

            for workspace_id in workspace_ids:
                ws_start = datetime.now(UTC)
                try:
                    member_user_ids = user_ids_by_workspace.get(workspace_id, [])
                    if not member_user_ids:
                        continue

                    async with session.begin():
                        summary_repo = WeeklySummaryRepository(session)
                        notification_repo = NotificationRepository(session)
                        notification_service = NotificationService(notification_repo)
                        service = WeeklySummaryService(summary_repo, session, notification_service)

                        primary_user_id = member_user_ids[0]
                        await asyncio.wait_for(
                            service.generate_for_workspace_week(
                                workspace_id, primary_user_id, last_monday
                            ),
                            timeout=WORKSPACE_EVALUATION_TIMEOUT_SECONDS,
                        )

                        for member_user_id in member_user_ids[1:]:
                            await notification_service.notify(
                                workspace_id=workspace_id,
                                user_id=member_user_id,
                                category="system",
                                severity="info",
                                title=f"Weekly summary ready: {last_monday.isoformat()}",
                                module="application",
                            )

                    duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                    logger.info(
                        "weekly_summary_workspace_success",
                        job_name="weekly_summary_job",
                        workspace_id=workspace_id,
                        duration_ms=duration_ms,
                        status="success",
                    )
                except TimeoutError:
                    duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                    logger.error(
                        "weekly_summary_workspace_timeout",
                        job_name="weekly_summary_job",
                        workspace_id=workspace_id,
                        duration_ms=duration_ms,
                        status="timeout",
                    )
                except Exception:
                    duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                    logger.error(
                        "weekly_summary_workspace_failed",
                        job_name="weekly_summary_job",
                        workspace_id=workspace_id,
                        duration_ms=duration_ms,
                        status="failed",
                        exc_info=True,
                    )

            total_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
            logger.info(
                "weekly_summary_job_completed",
                job_name="weekly_summary_job",
                duration_ms=total_ms,
                workspace_count=len(workspace_ids),
            )
        finally:
            await _release_session_advisory_lock(
                session, WEEKLY_SUMMARY_LOCK_KEY, "weekly_summary_job"
            )


FX_RATE_INGESTION_LOCK_KEY = ADVISORY_LOCK_FX_RATE_INGESTION
EXPORT_CLEANUP_LOCK_KEY = ADVISORY_LOCK_EXPORT_CLEANUP


async def fx_rate_ingestion_job() -> None:
    """
    Cron-triggered job that fetches and ingests the latest FX rates from ExchangeRate-API.

    Execution model:
      1. Acquire a Postgres advisory transaction lock to prevent concurrent execution
         during rolling deploys (key ADVISORY_LOCK_FX_RATE_INGESTION).
      2. Open a DB session and run the ingest_fx_rates workflow.
      3. Handle, log, and propagate errors cleanly.
    """
    start_time = datetime.now(UTC)
    logger.info("fx_rate_ingestion_job_start", job_name="fx_rate_ingestion_job")

    async with postgres.async_session_maker() as session, session.begin():
        lock_res = await session.execute(
            select(func.pg_try_advisory_xact_lock(FX_RATE_INGESTION_LOCK_KEY))
        )
        has_lock = lock_res.scalar()
        if not has_lock:
            logger.info(
                "fx_rate_ingestion_job_skipped_lock_held",
                job_name="fx_rate_ingestion_job",
            )
            return

        try:
            await ingest_fx_rates(session)

            total_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
            logger.info(
                "fx_rate_ingestion_job_completed",
                job_name="fx_rate_ingestion_job",
                duration_ms=total_ms,
            )
        except Exception as e:
            total_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
            logger.error(
                "fx_rate_ingestion_job_failed",
                job_name="fx_rate_ingestion_job",
                duration_ms=total_ms,
                error=str(e),
                exc_info=True,
            )
            raise e


async def export_cleanup_job() -> None:
    """
    Cron-triggered job that purges/marks expired exports (Spec 006).
    """
    start_time = datetime.now(UTC)
    logger.info("export_cleanup_job_start", job_name="export_cleanup_job")

    async with postgres.async_session_maker() as session, session.begin():
        lock_res = await session.execute(
            select(func.pg_try_advisory_xact_lock(EXPORT_CLEANUP_LOCK_KEY))
        )
        has_lock = lock_res.scalar()
        if not has_lock:
            logger.info(
                "export_cleanup_job_skipped_lock_held",
                job_name="export_cleanup_job",
            )
            return

        try:
            cleaned_count = await cleanup_expired_exports(session)

            total_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
            logger.info(
                "export_cleanup_job_completed",
                job_name="export_cleanup_job",
                duration_ms=total_ms,
                cleaned_count=cleaned_count,
            )
        except Exception:
            total_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
            logger.error(
                "export_cleanup_job_failed",
                job_name="export_cleanup_job",
                duration_ms=total_ms,
                status="failed",
                exc_info=True,
            )
            raise


SESSION_CLEANUP_LOCK_KEY = ADVISORY_LOCK_SESSION_CLEANUP
IMPORT_PREVIEW_CLEANUP_LOCK_KEY = ADVISORY_LOCK_IMPORT_PREVIEW_CLEANUP


async def session_cleanup_job() -> None:
    """
    Cron-triggered job that purges expired and revoked authentication sessions (Spec 003).
    """
    start_time = datetime.now(UTC)
    logger.info("session_cleanup_job_start", job_name="session_cleanup_job")

    async with postgres.async_session_maker() as session, session.begin():
        lock_res = await session.execute(
            select(func.pg_try_advisory_xact_lock(SESSION_CLEANUP_LOCK_KEY))
        )
        has_lock = lock_res.scalar()
        if not has_lock:
            logger.info(
                "session_cleanup_job_skipped_lock_held",
                job_name="session_cleanup_job",
            )
            return

        try:
            cleaned_count = await cleanup_expired_sessions(session)

            total_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
            logger.info(
                "session_cleanup_job_completed",
                job_name="session_cleanup_job",
                duration_ms=total_ms,
                cleaned_count=cleaned_count,
            )
        except Exception:
            total_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
            logger.error(
                "session_cleanup_job_failed",
                job_name="session_cleanup_job",
                duration_ms=total_ms,
                status="failed",
                exc_info=True,
            )
            raise


async def import_preview_cleanup_job() -> None:
    """
    Cron-triggered job that purges stale import preview rows (Spec 020).
    """
    start_time = datetime.now(UTC)
    logger.info("import_preview_cleanup_job_start", job_name="import_preview_cleanup_job")

    async with postgres.async_session_maker() as session, session.begin():
        lock_res = await session.execute(
            select(func.pg_try_advisory_xact_lock(IMPORT_PREVIEW_CLEANUP_LOCK_KEY))
        )
        has_lock = lock_res.scalar()
        if not has_lock:
            logger.info(
                "import_preview_cleanup_job_skipped_lock_held",
                job_name="import_preview_cleanup_job",
            )
            return

        try:
            cleaned_count = await cleanup_import_previews(session)

            total_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
            logger.info(
                "import_preview_cleanup_job_completed",
                job_name="import_preview_cleanup_job",
                duration_ms=total_ms,
                cleaned_count=cleaned_count,
            )
        except Exception:
            total_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
            logger.error(
                "import_preview_cleanup_job_failed",
                job_name="import_preview_cleanup_job",
                duration_ms=total_ms,
                status="failed",
                exc_info=True,
            )
            raise


# Advisory lock key — see app.core.constants for the full registry
PUSH_DELIVERY_LOCK_KEY = ADVISORY_LOCK_PUSH_DELIVERY


async def push_delivery_job() -> None:
    """Cron-triggered job that drains pending push-channel NotificationDelivery
    rows (spec-052). Global, not per-workspace — a delivery queue has no
    natural workspace-iteration shape, so this follows fx_rate_ingestion_job's
    single-lock/single-session pattern rather than run_workspace_job."""
    if (
        not settings.VAPID_PRIVATE_KEY
        or not settings.VAPID_PUBLIC_KEY
        or not settings.VAPID_SUBJECT
    ):
        return

    start_time = datetime.now(UTC)
    logger.info("push_delivery_job_start", job_name="push_delivery_job")

    async with postgres.async_session_maker() as session, session.begin():
        lock_res = await session.execute(
            select(func.pg_try_advisory_xact_lock(PUSH_DELIVERY_LOCK_KEY))
        )
        has_lock = lock_res.scalar()
        if not has_lock:
            logger.info("push_delivery_job_skipped_lock_held", job_name="push_delivery_job")
            return

        try:
            counts = await deliver_pending_push_notifications(session)

            total_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
            logger.info(
                "push_delivery_job_completed",
                job_name="push_delivery_job",
                duration_ms=total_ms,
                **counts,
            )
        except Exception:
            total_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
            logger.error(
                "push_delivery_job_failed",
                job_name="push_delivery_job",
                duration_ms=total_ms,
                status="failed",
                exc_info=True,
            )
            raise


# Advisory lock key — see app.core.constants for the full registry
TODO_REMINDER_LOCK_KEY = ADVISORY_LOCK_TODO_REMINDER


async def todo_reminder_job(workspace_id: int | None = None) -> None:
    """Cron-triggered job that creates due-reminder Notifications for
    incomplete todos (spec-052) — the first real notification source that
    makes push delivery worth having. Idempotent via Todo.reminded_at."""
    window_end = datetime.now(UTC) + timedelta(minutes=settings.TODO_REMINDER_INTERVAL_MINUTES)

    await run_workspace_job(
        job_name="todo_reminder_job",
        lock_key=TODO_REMINDER_LOCK_KEY,
        workspace_id=workspace_id,
        process_workspace=lambda s, ws: process_workspace_todo_reminders(s, ws, window_end),
    )


# Advisory lock key — see app.core.constants for the full registry
MEDICATION_REMINDER_LOCK_KEY = ADVISORY_LOCK_MEDICATION_REMINDER


async def medication_reminder_job(workspace_id: int | None = None) -> None:
    """Cron-triggered job that creates due-reminder Notifications for
    medication dose slots (spec-069) — clone of todo_reminder_job. Idempotent
    via Medication.last_reminded_slot."""
    now = datetime.now(UTC)
    window_end = now + timedelta(minutes=settings.HEALTH_REMINDER_INTERVAL_MINUTES)

    await run_workspace_job(
        job_name="medication_reminder_job",
        lock_key=MEDICATION_REMINDER_LOCK_KEY,
        workspace_id=workspace_id,
        process_workspace=lambda s, ws: process_workspace_medication_reminders(
            s, ws, now, window_end
        ),
    )


# Advisory lock key — see app.core.constants for the full registry
NET_WORTH_SNAPSHOT_LOCK_KEY = ADVISORY_LOCK_NET_WORTH_SNAPSHOT


async def net_worth_snapshot_job(workspace_id: int | None = None) -> None:
    """Cron-triggered job that materializes the daily net worth snapshot
    for each active workspace (spec-065)."""

    async def _process_workspace(session: AsyncSession, workspace: Workspace) -> None:
        from app.finance.repository import (  # noqa: PLC0415
            AccountRepository,
            CurrencyRepository,
            FinanceSettingRepository,
            FxRateRepository,
            NetWorthSnapshotRepository,
        )
        from app.finance.service import AccountService, NetWorthService  # noqa: PLC0415
        from app.investing.performance_service import InvestingSummaryService  # noqa: PLC0415
        from app.investing.repository import CashBalanceRepository  # noqa: PLC0415

        account_repo = AccountRepository(session)
        currency_repo = CurrencyRepository(session)
        setting_repo = FinanceSettingRepository(session)
        fx_rate_repo = FxRateRepository(session)
        net_worth_snapshot_repo = NetWorthSnapshotRepository(session)
        cash_balance_repo = CashBalanceRepository(session)

        account_service = AccountService(
            account_repository=account_repo,
            currency_repository=currency_repo,
            setting_repository=setting_repo,
        )

        from app.investing.repository import (  # noqa: PLC0415
            HoldingPriceRepository,
            HoldingRepository,
            PortfolioSnapshotRepository,
        )

        holding_repo = HoldingRepository(session)
        holding_price_repo = HoldingPriceRepository(session)
        snapshot_repo = PortfolioSnapshotRepository(session)

        summary_service = InvestingSummaryService(
            holding_repo=holding_repo,
            cash_repo=cash_balance_repo,
            finance_setting_repo=setting_repo,
            fx_rate_repo=fx_rate_repo,
            holding_price_repo=holding_price_repo,
            snapshot_repo=snapshot_repo,
            account_repo=account_repo,
        )

        net_worth_service = NetWorthService(
            session=session,
            account_service=account_service,
            summary_service=summary_service,
            cash_balance_repo=cash_balance_repo,
            setting_repo=setting_repo,
            fx_rate_repo=fx_rate_repo,
            net_worth_snapshot_repo=net_worth_snapshot_repo,
        )

        today_date = datetime.now(UTC).date()
        await net_worth_service.create_net_worth_snapshot(workspace.id, today_date)

    await run_workspace_job(
        job_name="net_worth_snapshot_job",
        lock_key=NET_WORTH_SNAPSHOT_LOCK_KEY,
        workspace_id=workspace_id,
        process_workspace=_process_workspace,
    )


# Advisory lock key — see app.core.constants for the full registry
MORNING_BRIEFING_LOCK_KEY = ADVISORY_LOCK_MORNING_BRIEFING

_BRIEFING_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


async def morning_briefing_job(workspace_id: int | None = None) -> None:
    """Cron-triggered job that composes each workspace's morning briefing
    (spec-067) and, if not ``all_clear``, writes ONE ``Notification``
    (category="briefing", severity = the briefing's most severe line, body =
    its top 3 line texts). All-clear workspaces get nothing — calm by
    default. Push delivery for this category defaults ON for users with an
    active push subscription even absent an explicit preference row (see
    ``NotificationService.notify``'s "briefing" special case)."""

    async def _process_workspace(session: AsyncSession, workspace: Workspace) -> None:
        if workspace.id is None:
            return

        members_res = await session.execute(
            select(WorkspaceMembership.user_id)
            .where(WorkspaceMembership.workspace_id == workspace.id)
            .order_by(
                (WorkspaceMembership.role == "owner").desc(), WorkspaceMembership.created_at.asc()
            )
            .limit(1)
        )
        user_id = members_res.scalar()
        if not user_id:
            logger.warning("morning_briefing_no_members", workspace_id=workspace.id)
            return

        category_repo = CategoryRepository(session)
        category_group_repo = CategoryGroupRepository(session)
        budget_repo = BudgetRepository(session)
        finance_setting_repo = FinanceSettingRepository(session)
        fx_rate_repo = FxRateRepository(session)
        account_repo = AccountRepository(session)
        instrument_repo = InstrumentRepository(session)
        holding_repo = HoldingRepository(session)
        cash_repo = CashBalanceRepository(session)
        holding_price_repo = HoldingPriceRepository(session)
        snapshot_repo = PortfolioSnapshotRepository(session)
        notification_repo = NotificationRepository(session)
        notification_service = NotificationService(notification_repo)

        workflow = MorningBriefingWorkflow(
            todo_service=TodoService(TodoRepository(session)),
            budget_service=BudgetService(budget_repo, category_repo, category_group_repo),
            investing_performance_service=PerformanceService(
                holding_repo,
                cash_repo,
                holding_price_repo,
                snapshot_repo,
                finance_setting_repo,
                fx_rate_repo,
                instrument_repo,
                account_repo,
            ),
            recurring_transaction_service=RecurringTransactionService(
                RecurringTransactionRepository(session),
                TransactionRepository(session),
                category_repo,
            ),
            notification_service=notification_service,
            import_repo=ImportRepository(session),
            weekly_summary_repo=WeeklySummaryRepository(session),
            finance_setting_repo=finance_setting_repo,
            health_service=HealthService(
                MedicationRepository(session),
                MedicationEventRepository(session),
                WeightEntryRepository(session),
            ),
        )

        briefing = await workflow.get_briefing(workspace.id, user_id)
        if briefing.all_clear:
            return

        top_severity = min(
            (line.severity for line in briefing.lines),
            key=lambda severity: _BRIEFING_SEVERITY_RANK.get(severity, 3),
        )
        body = " · ".join(line.text for line in briefing.lines[:3])
        await notification_service.notify(
            workspace_id=workspace.id,
            user_id=user_id,
            category="briefing",
            severity=top_severity,
            title="Morning briefing",
            body=body,
            module="application",
        )

    await run_workspace_job(
        job_name="morning_briefing_job",
        lock_key=MORNING_BRIEFING_LOCK_KEY,
        process_workspace=_process_workspace,
        workspace_id=workspace_id,
    )
