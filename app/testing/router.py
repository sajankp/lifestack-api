from datetime import UTC, datetime
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
    get_workspace_repo,
    require_min_role,
)
from app.core.exceptions import NotFoundError
from app.platform.repository import WorkspaceRepository
from app.spending.models import RecurringTransaction

router = APIRouter(prefix="/e2e", tags=["testing"])


class WorkflowRunResponse(BaseModel):
    status: str = "ok"
    generated_count: int | None = None


class RecurringTransactionTriggerRequest(BaseModel):
    description: str = Field(..., min_length=1, max_length=500)


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
