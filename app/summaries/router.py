import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import (
    get_current_user,
    get_current_workspace_id,
    get_weekly_summary_service,
    require_min_role,
)
from app.core.pagination import PaginatedResponse, PaginationParams, build_page
from app.summaries.schemas import WeeklySummaryResponse
from app.summaries.service import WeeklySummaryService

router = APIRouter(
    prefix="/summaries/weekly",
    tags=["summaries"],
    dependencies=[Depends(require_min_role("member"))],
)


@router.get("", response_model=PaginatedResponse[WeeklySummaryResponse])
async def list_weekly_summaries(
    service: Annotated[WeeklySummaryService, Depends(get_weekly_summary_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"),
):
    items, total = await service.list(
        workspace_id, from_date, to_date, pagination.limit, pagination.offset
    )
    return build_page([WeeklySummaryResponse.model_validate(i) for i in items], total, pagination)


@router.get("/latest", response_model=WeeklySummaryResponse)
async def get_latest_weekly_summary(
    service: Annotated[WeeklySummaryService, Depends(get_weekly_summary_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    return WeeklySummaryResponse.model_validate(await service.latest(workspace_id))


@router.get("/{summary_id}", response_model=WeeklySummaryResponse)
async def get_weekly_summary(
    summary_id: uuid.UUID,
    service: Annotated[WeeklySummaryService, Depends(get_weekly_summary_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    return WeeklySummaryResponse.model_validate(await service.get(workspace_id, summary_id))


@router.post("/{summary_id}/read", response_model=WeeklySummaryResponse)
async def mark_weekly_summary_read(
    summary_id: uuid.UUID,
    service: Annotated[WeeklySummaryService, Depends(get_weekly_summary_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Mark a summary as read (spec-080) so the morning-briefing 'ready' line
    clears. Idempotent; workspace-scoped 404 for unknown ids."""
    return WeeklySummaryResponse.model_validate(await service.mark_read(workspace_id, summary_id))
