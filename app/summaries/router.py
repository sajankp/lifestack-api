import uuid
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import (
    get_current_user,
    get_current_workspace_id,
    get_summary_settings_service,
    get_weekly_summary_service,
    require_min_role,
)
from app.core.pagination import PaginatedResponse, PaginationParams, build_page
from app.summaries.schemas import (
    RegenerateWeeklySummaryRequest,
    WeeklySummaryResponse,
    WorkspaceSummarySettingResponse,
    WorkspaceSummarySettingUpdate,
)
from app.summaries.service import SummarySettingsService, WeeklySummaryService

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
    return build_page([WeeklySummaryResponse.from_summary(i) for i in items], total, pagination)


@router.get("/settings", response_model=WorkspaceSummarySettingResponse)
async def get_summary_cadence_settings(
    service: Annotated[SummarySettingsService, Depends(get_summary_settings_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Per-workspace weekly-summary cadence (spec-076). No row yet -> the
    documented default (Monday, hour 1 UTC) that the job itself falls back to."""
    row = await service.get(workspace_id)
    if row is None:
        return WorkspaceSummarySettingResponse(
            cadence_day_of_week=0,
            cadence_hour_utc=1,
            updated_at=datetime.now(UTC),
        )
    return WorkspaceSummarySettingResponse.model_validate(row)


@router.put("/settings", response_model=WorkspaceSummarySettingResponse)
async def update_summary_cadence_settings(
    setting_in: WorkspaceSummarySettingUpdate,
    service: Annotated[SummarySettingsService, Depends(get_summary_settings_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("admin"))],
):
    row = await service.update(
        workspace_id, setting_in.cadence_day_of_week, setting_in.cadence_hour_utc
    )
    return WorkspaceSummarySettingResponse.model_validate(row)


@router.get("/latest", response_model=WeeklySummaryResponse)
async def get_latest_weekly_summary(
    service: Annotated[WeeklySummaryService, Depends(get_weekly_summary_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    item = await service.latest(workspace_id)
    return WeeklySummaryResponse.from_summary(
        item,
        data_revised_after_snapshot=await service.has_reverted_import_overlap(item),
        data_stale=await service.is_stale(item),
    )


@router.get("/{summary_id}", response_model=WeeklySummaryResponse)
async def get_weekly_summary(
    summary_id: uuid.UUID,
    service: Annotated[WeeklySummaryService, Depends(get_weekly_summary_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    item = await service.get(workspace_id, summary_id)
    return WeeklySummaryResponse.from_summary(
        item,
        data_revised_after_snapshot=await service.has_reverted_import_overlap(item),
        data_stale=await service.is_stale(item),
    )


@router.post("/{summary_id}/read", response_model=WeeklySummaryResponse)
async def mark_weekly_summary_read(
    summary_id: uuid.UUID,
    service: Annotated[WeeklySummaryService, Depends(get_weekly_summary_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Mark a summary as read (spec-080) so the morning-briefing 'ready' line
    clears. Idempotent; workspace-scoped 404 for unknown ids."""
    return WeeklySummaryResponse.from_summary(await service.mark_read(workspace_id, summary_id))


@router.post("/{summary_id}/regenerate", response_model=WeeklySummaryResponse)
async def regenerate_weekly_summary(
    summary_id: uuid.UUID,
    regenerate_in: RegenerateWeeklySummaryRequest,
    service: Annotated[WeeklySummaryService, Depends(get_weekly_summary_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Recompute the same week from current data (spec-076). The superseded
    summary is retained (never deleted) and marked accordingly; this does
    NOT send a notification — a regenerate is a correction, not a new event.
    404 if the id is unknown/other-workspace, or already superseded (a
    summary can only be regenerated once per version — regenerate the latest
    version instead)."""
    new = await service.regenerate(workspace_id, summary_id, regenerate_in.reason)
    return WeeklySummaryResponse.from_summary(new)
