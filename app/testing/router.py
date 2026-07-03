from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.application.workflows import (
    evaluate_workspace_budget_guardrails,
    process_workspace_recurring_transactions,
)
from app.core.dependencies import (
    get_current_user,
    get_current_workspace_id,
    get_db_session,
    get_finance_fx_rate_service,
    get_workspace_repo,
    require_min_role,
)
from app.core.exceptions import NotFoundError
from app.finance.schemas import FxRateUpsert
from app.finance.service import FxRateService
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService
from app.platform.repository import WorkspaceRepository
from app.spending.models import RecurringTransaction
from app.summaries.repository import WeeklySummaryRepository
from app.summaries.service import WeeklySummaryService

router = APIRouter(prefix="/e2e", tags=["testing"])


class WorkflowRunResponse(BaseModel):
    status: str = "ok"
    generated_count: int | None = None


class WeeklySummaryWorkflowRunResponse(BaseModel):
    status: str = "ok"
    summary_public_id: str
    week_start: date
    week_end: date


class RecurringTransactionTriggerRequest(BaseModel):
    description: str = Field(..., min_length=1, max_length=500)


class WeeklySummaryTriggerRequest(BaseModel):
    week_start: date | None = None


class FxRateSeedRequest(BaseModel):
    base_currency_code: str = Field(..., min_length=1, max_length=10)
    quote_currency_code: str = Field(..., min_length=1, max_length=10)
    rate: Decimal = Field(..., gt=0, decimal_places=10)


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
    await fx_rate_service.upsert(
        FxRateUpsert(
            base_currency_code=payload.base_currency_code,
            quote_currency_code=payload.quote_currency_code,
            rate=payload.rate,
            as_of=now,
            fetched_at=now,
            source="e2e_seed",
        )
    )
    return WorkflowRunResponse()


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
