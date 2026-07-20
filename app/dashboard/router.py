from typing import Annotated

from fastapi import APIRouter, Depends

from app.application.workflows import DashboardSummaryWorkflow, MorningBriefingWorkflow
from app.config import settings
from app.core.cache import ResponseCache
from app.core.dependencies import (
    get_current_user,
    get_current_workspace_id,
    get_dashboard_summary_workflow,
    get_morning_briefing_workflow,
    get_response_cache,
)
from app.dashboard.schemas import BriefingResponse, DashboardSummary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary)
async def get_summary(
    dashboard_workflow: Annotated[
        DashboardSummaryWorkflow, Depends(get_dashboard_summary_workflow)
    ],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    cache: Annotated[ResponseCache, Depends(get_response_cache)],
):
    key = f"cache:v1:dashboard:summary:{workspace_id}"
    if cached := await cache.get_json(key):
        return cached
    result = await dashboard_workflow.get_summary(workspace_id)
    await cache.set_json(
        key, result.model_dump(mode="json"), ttl_seconds=settings.DASHBOARD_CACHE_TTL_SECONDS
    )
    return result


@router.get("/briefing", response_model=BriefingResponse)
async def get_briefing(
    briefing_workflow: Annotated[MorningBriefingWorkflow, Depends(get_morning_briefing_workflow)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    current_user: Annotated[dict, Depends(get_current_user)],
):
    return await briefing_workflow.get_briefing(workspace_id, current_user["id"])
