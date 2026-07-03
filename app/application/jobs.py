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
    cleanup_expired_exports,
    cleanup_expired_sessions,
    cleanup_import_previews,
    deliver_pending_push_notifications,
    evaluate_workspace_budget_guardrails,
    ingest_fx_rates,
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
    ADVISORY_LOCK_PUSH_DELIVERY,
    ADVISORY_LOCK_RECURRING_TRANSACTIONS,
    ADVISORY_LOCK_SESSION_CLEANUP,
    ADVISORY_LOCK_TODO_REMINDER,
    ADVISORY_LOCK_WEEKLY_SUMMARY,
)
from app.core.database import postgres
from app.finance.repository import FinanceSettingRepository, FxRateRepository
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
from app.summaries.repository import WeeklySummaryRepository
from app.summaries.service import WeeklySummaryService

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

    Mirrors the scaffolding shared by the per-workspace cron jobs exactly:
    acquire a Postgres advisory transaction lock, fetch the active workspace
    list (optionally scoped to a single ``workspace_id``), then evaluate each
    workspace in its own isolated, timeout-bounded transaction — one
    workspace's failure is logged and skipped, others continue. Log event
    names (``{job_name}_start``, ``_skipped_lock_held``,
    ``_workspace_not_found_or_inactive``, ``_workspace_success``,
    ``_workspace_timeout``, ``_workspace_failed``, ``_completed``) match what
    the jobs logged before extraction — do not rename them without checking
    ``test_scheduler.py`` and any log-based alerting.
    """
    start_time = datetime.now(UTC)
    logger.info(f"{job_name}_start", job_name=job_name)

    async with postgres.async_session_maker() as session, session.begin():
        lock_res = await session.execute(select(func.pg_try_advisory_xact_lock(lock_key)))
        has_lock = lock_res.scalar()
        if not has_lock:
            logger.info(f"{job_name}_skipped_lock_held", job_name=job_name)
            return

        if workspace_id is not None:
            workspaces_res = await session.execute(
                select(Workspace).where(Workspace.id == workspace_id, Workspace.is_active)
            )
        else:
            workspaces_res = await session.execute(select(Workspace).where(Workspace.is_active))
        workspaces = workspaces_res.scalars().all()
        if workspace_id is not None and not workspaces:
            logger.warning(
                f"{job_name}_workspace_not_found_or_inactive",
                workspace_id=workspace_id,
            )

        for workspace in workspaces:
            ws_start = datetime.now(UTC)
            try:
                async with postgres.async_session_maker() as ws_session:  # noqa: SIM117
                    async with ws_session.begin():
                        await asyncio.wait_for(
                            process_workspace(ws_session, workspace),
                            timeout=timeout_seconds,
                        )

                duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                logger.info(
                    f"{job_name}_workspace_success",
                    job_name=job_name,
                    workspace_id=workspace.id,
                    duration_ms=duration_ms,
                    status="success",
                )
            except TimeoutError:
                duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                logger.error(
                    f"{job_name}_workspace_timeout",
                    job_name=job_name,
                    workspace_id=workspace.id,
                    duration_ms=duration_ms,
                    status="timeout",
                )
            except Exception:
                duration_ms = (datetime.now(UTC) - ws_start).total_seconds() * 1000
                logger.error(
                    f"{job_name}_workspace_failed",
                    job_name=job_name,
                    workspace_id=workspace.id,
                    duration_ms=duration_ms,
                    status="failed",
                    exc_info=True,
                )

        total_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
        logger.info(
            f"{job_name}_completed",
            job_name=job_name,
            duration_ms=total_ms,
            workspace_count=len(workspaces),
        )


async def investment_closing_prices_job() -> None:
    """Cache each workspace's latest completed market close once per day."""
    async with postgres.async_session_maker() as session:
        workspace_ids = (
            (await session.execute(select(Workspace.id).where(Workspace.is_active))).scalars().all()
        )

    for workspace_id in workspace_ids:
        try:
            async with postgres.async_session_maker() as session, session.begin():
                service = PerformanceService(
                    HoldingRepository(session),
                    CashBalanceRepository(session),
                    HoldingPriceRepository(session),
                    PortfolioSnapshotRepository(session),
                    FinanceSettingRepository(session),
                    FxRateRepository(session),
                    InstrumentRepository(session),
                )
                updated = await service.refresh_workspace_prices(workspace_id)
                logger.info(
                    "investment_closing_prices_workspace_completed",
                    workspace_id=workspace_id,
                    updated_symbols=sorted(updated),
                )
        except Exception:
            logger.error(
                "investment_closing_prices_workspace_failed",
                workspace_id=workspace_id,
                exc_info=True,
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

    async with postgres.engine.connect() as conn, conn.begin():
        lock_res = await conn.execute(
            select(func.pg_try_advisory_xact_lock(RECURRING_TRANSACTIONS_LOCK_KEY))
        )
        has_lock = lock_res.scalar()
        if not has_lock:
            logger.info(
                "recurring_transactions_job_skipped_lock_held",
                job_name="recurring_transactions_job",
            )
            return

        async with postgres.async_session_maker() as session:
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

        total_generated = 0
        total_todos_generated = 0
        for ws_id in workspace_ids:
            ws_start = datetime.now(UTC)
            try:
                async with postgres.async_session_maker() as ws_session:  # noqa: SIM117
                    async with ws_session.begin():
                        workspace = await ws_session.get(Workspace, ws_id)
                        if workspace is None or not workspace.is_active:
                            continue
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

    async with postgres.engine.connect() as conn:
        lock_res = await conn.execute(select(func.pg_try_advisory_lock(WEEKLY_SUMMARY_LOCK_KEY)))
        has_lock = lock_res.scalar()
        if not has_lock:
            logger.info(
                "weekly_summary_job_skipped_lock_held",
                job_name="weekly_summary_job",
            )
            return

        try:
            async with postgres.async_session_maker() as session:
                if workspace_id is not None:
                    workspaces_res = await session.execute(
                        select(Workspace).where(Workspace.id == workspace_id, Workspace.is_active)
                    )
                else:
                    workspaces_res = await session.execute(
                        select(Workspace).where(Workspace.is_active)
                    )
                workspaces = list(workspaces_res.scalars().all())
                if workspace_id is not None and not workspaces:
                    logger.warning(
                        "weekly_summary_job_workspace_not_found_or_inactive",
                        workspace_id=workspace_id,
                    )
                workspace_ids = [
                    workspace.id for workspace in workspaces if workspace.id is not None
                ]
                memberships_by_workspace: dict[int, list[WorkspaceMembership]] = {}
                if workspace_ids:
                    memberships_res = await session.execute(
                        select(WorkspaceMembership).where(
                            WorkspaceMembership.workspace_id.in_(workspace_ids)
                        )
                    )
                    for membership in memberships_res.scalars().all():
                        memberships_by_workspace.setdefault(membership.workspace_id, []).append(
                            membership
                        )
                    for memberships in memberships_by_workspace.values():
                        memberships.sort(
                            key=lambda membership: WEEKLY_SUMMARY_ROLE_ORDER.get(
                                membership.role, 99
                            )
                        )

            # Calculate week_start as the Monday of the previous week
            if week_start is None:
                today = start_time.date()
                days_since_monday = today.weekday()
                last_monday = today - timedelta(days=days_since_monday + 7)
            else:
                last_monday = week_start

            for workspace in workspaces:
                workspace_id = workspace.id
                if workspace_id is None:
                    continue

                ws_start = datetime.now(UTC)
                try:
                    memberships = memberships_by_workspace.get(workspace_id, [])
                    if not memberships:
                        continue

                    async with postgres.async_session_maker() as ws_session, ws_session.begin():
                        summary_repo = WeeklySummaryRepository(ws_session)
                        notification_repo = NotificationRepository(ws_session)
                        notification_service = NotificationService(notification_repo)
                        service = WeeklySummaryService(
                            summary_repo, ws_session, notification_service
                        )

                        primary_user_id = memberships[0].user_id
                        await asyncio.wait_for(
                            service.generate_for_workspace_week(
                                workspace_id, primary_user_id, last_monday
                            ),
                            timeout=WORKSPACE_EVALUATION_TIMEOUT_SECONDS,
                        )

                        for membership in memberships[1:]:
                            await notification_service.notify(
                                workspace_id=workspace_id,
                                user_id=membership.user_id,
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
                workspace_count=len(workspaces),
            )
        finally:
            await conn.execute(select(func.pg_advisory_unlock(WEEKLY_SUMMARY_LOCK_KEY)))


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
    if not settings.VAPID_PRIVATE_KEY or not settings.VAPID_PUBLIC_KEY:
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
