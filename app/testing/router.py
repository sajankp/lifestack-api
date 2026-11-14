import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.application.workflows import (
    evaluate_workspace_budget_guardrails,
    process_workspace_recurring_transactions,
)
from app.core.currency import effective_display_as_of
from app.core.dependencies import (
    get_current_user,
    get_current_workspace_id,
    get_db_session,
    get_finance_fx_rate_service,
    get_investing_portfolio_report_service,
    get_workspace_repo,
    require_min_role,
)
from app.core.exceptions import NotFoundError
from app.finance.schemas import FxRateUpsert
from app.finance.service import FxRateService
from app.investing.service import PortfolioReportService
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService
from app.platform.repository import WorkspaceRepository
from app.spending.models import RecurringTransaction
from app.summaries.repository import MonthlySummaryRepository, WeeklySummaryRepository
from app.summaries.service import MonthlySummaryService, WeeklySummaryService

router = APIRouter(prefix="/e2e", tags=["testing"])


class WorkflowRunResponse(BaseModel):
    status: str = "ok"
    generated_count: int | None = None


class WeeklySummaryWorkflowRunResponse(BaseModel):
    status: str = "ok"
    summary_public_id: str
    week_start: date
    week_end: date


class MonthlySummaryWorkflowRunResponse(BaseModel):
    status: str = "ok"
    summary_public_id: str
    year: int
    month: int


class RecurringTransactionTriggerRequest(BaseModel):
    description: str = Field(..., min_length=1, max_length=500)


class WeeklySummaryTriggerRequest(BaseModel):
    week_start: date | None = None


class MonthlySummaryTriggerRequest(BaseModel):
    year: int | None = None
    month: int | None = None


class FxRateSeedRequest(BaseModel):
    base_currency_code: str = Field(..., min_length=1, max_length=10)
    quote_currency_code: str = Field(..., min_length=1, max_length=10)
    rate: Decimal = Field(..., gt=0, decimal_places=10)


class PortfolioReportSeedRequest(BaseModel):
    """Mirrors the ``create_portfolio_report`` MCP tool's denormalised columns."""

    as_of: date
    run_id: str = Field(
        default_factory=lambda: f"e2e-{uuid.uuid4().hex[:8]}", min_length=1, max_length=128
    )
    theme_weights: dict[str, Any] = Field(default_factory=dict)
    harvest_candidates: list[Any] = Field(default_factory=list)
    lrs_headroom_inr: Decimal | None = None
    tax_buckets: dict[str, Any] = Field(default_factory=dict)
    foundation_json: dict[str, Any] | None = None


class PortfolioReportSeedResponse(BaseModel):
    status: str = "ok"
    public_id: str


async def _active_workspace_or_404(workspace_id: int, workspace_repo: WorkspaceRepository):
    workspace = await workspace_repo.get_by_id(workspace_id)
    if workspace is None or not workspace.is_active:
        raise NotFoundError(detail="Workspace not found or is inactive")
    return workspace


@router.post("/workflows/budget-guardrails", response_model=WorkflowRunResponse)
async def trigger_budget_guardrails(
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("owner"))],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    workspace_repo: Annotated[WorkspaceRepository, Depends(get_workspace_repo)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> WorkflowRunResponse:
    workspace = await _active_workspace_or_404(workspace_id, workspace_repo)
    await evaluate_workspace_budget_guardrails(session, workspace)
    return WorkflowRunResponse()


@router.post("/workflows/recurring-transactions", response_model=WorkflowRunResponse)
async def trigger_recurring_transactions(
    payload: RecurringTransactionTriggerRequest,
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("owner"))],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    workspace_repo: Annotated[WorkspaceRepository, Depends(get_workspace_repo)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> WorkflowRunResponse:
    workspace = await _active_workspace_or_404(workspace_id, workspace_repo)
    result = await session.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.workspace_id == workspace_id,
            RecurringTransaction.description == payload.description,
            RecurringTransaction.is_active == True,  # noqa: E712
        )
    )
    recurrence = result.scalar_one_or_none()
    if recurrence is None:
        raise NotFoundError(detail="Recurring rule not found")

    recurrence.next_due_date = datetime.now(UTC).date()
    session.add(recurrence)
    await session.flush()

    generated_count = await process_workspace_recurring_transactions(session, workspace)
    return WorkflowRunResponse(generated_count=generated_count)


@router.post("/fx-rates", response_model=WorkflowRunResponse)
async def seed_fx_rate(
    payload: FxRateSeedRequest,
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("owner"))],
    fx_rate_service: Annotated[FxRateService, Depends(get_finance_fx_rate_service)],
) -> WorkflowRunResponse:
    """
    Seed a globally scoped FX rate for e2e tests.

    Normal ingestion (`ingest_fx_rates`) requires a live ExchangeRate-API call and
    EXCHANGERATE_API_KEY, neither available in the e2e stack, so this hook lets
    FX-dependent flows (reporting-currency conversion, look-through analytics) be
    tested deterministically without hitting a real external API.
    """
    now = datetime.now(UTC)
    # Display conversions only honor a rate dated on or before the *previous*
    # calendar day's close (spec-075's effective_display_as_of) -- seeding
    # as_of=now would never be picked up by any valuation lookup, so backdate
    # it to satisfy that cutoff.
    as_of = effective_display_as_of(now)
    await fx_rate_service.upsert(
        FxRateUpsert(
            base_currency_code=payload.base_currency_code,
            quote_currency_code=payload.quote_currency_code,
            rate=payload.rate,
            as_of=as_of,
            fetched_at=now,
            source="e2e_seed",
        )
    )
    return WorkflowRunResponse()


@router.post("/portfolio-reports", response_model=PortfolioReportSeedResponse)
async def seed_portfolio_report(
    payload: PortfolioReportSeedRequest,
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("owner"))],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    report_service: Annotated[
        PortfolioReportService, Depends(get_investing_portfolio_report_service)
    ],
) -> PortfolioReportSeedResponse:
    """
    Seed one portfolio report row for the caller's workspace.

    Reports are normally written by the fortnightly cron through the
    ``create_portfolio_report`` MCP tool, which the e2e stack cannot reach, so this
    hook lets the Analytics report cards be exercised against the real stored shape.
    """
    headroom = (
        payload.lrs_headroom_inr.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if payload.lrs_headroom_inr is not None
        else None
    )
    report = await report_service.create_portfolio_report(
        workspace_id=workspace_id,
        as_of=payload.as_of,
        run_id=payload.run_id,
        foundation_json=payload.foundation_json,
        theme_weights=payload.theme_weights,
        harvest_candidates=payload.harvest_candidates,
        lrs_headroom_inr=headroom,
        tax_buckets=payload.tax_buckets,
    )
    return PortfolioReportSeedResponse(public_id=str(report.public_id))


@router.post("/workflows/weekly-summary", response_model=WeeklySummaryWorkflowRunResponse)
async def trigger_weekly_summary(
    payload: WeeklySummaryTriggerRequest,
    user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("owner"))],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    workspace_repo: Annotated[WorkspaceRepository, Depends(get_workspace_repo)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> WeeklySummaryWorkflowRunResponse:
    await _active_workspace_or_404(workspace_id, workspace_repo)
    today = datetime.now(UTC).date()
    default_week_start = today - timedelta(days=today.weekday() + 7)
    service = WeeklySummaryService(
        WeeklySummaryRepository(session),
        session,
        NotificationService(NotificationRepository(session)),
    )
    summary = await service.generate_for_workspace_week(
        workspace_id, user["id"], payload.week_start or default_week_start
    )
    return WeeklySummaryWorkflowRunResponse(
        summary_public_id=str(summary.public_id),
        week_start=summary.week_start,
        week_end=summary.week_end,
    )


@router.post("/workflows/monthly-summary", response_model=MonthlySummaryWorkflowRunResponse)
async def trigger_monthly_summary(
    payload: MonthlySummaryTriggerRequest,
    user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("owner"))],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    workspace_repo: Annotated[WorkspaceRepository, Depends(get_workspace_repo)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> MonthlySummaryWorkflowRunResponse:
    await _active_workspace_or_404(workspace_id, workspace_repo)
    today = datetime.now(UTC).date()
    if today.month == 1:
        default_year = today.year - 1
        default_month = 12
    else:
        default_year = today.year
        default_month = today.month - 1

    target_year = payload.year or default_year
    target_month = payload.month or default_month

    service = MonthlySummaryService(
        MonthlySummaryRepository(session),
        session,
        NotificationService(NotificationRepository(session)),
    )
    month_start = date(target_year, target_month, 1)
    summary = await service.generate_for_workspace_month(workspace_id, user["id"], month_start)
    return MonthlySummaryWorkflowRunResponse(
        summary_public_id=str(summary.public_id),
        year=summary.month_start.year,
        month=summary.month_start.month,
    )
