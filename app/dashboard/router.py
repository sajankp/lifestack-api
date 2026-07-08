from typing import Annotated

from fastapi import APIRouter, Depends

from app.application.workflows import DashboardSummaryWorkflow, MorningBriefingWorkflow
from app.core.dependencies import (
    get_current_user,
    get_current_workspace_id,
    get_dashboard_summary_workflow,
    get_morning_briefing_workflow,
)
from app.dashboard.schemas import BriefingResponse, DashboardSummary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary)
async def get_summary(
    dashboard_workflow: Annotated[
        DashboardSummaryWorkflow, Depends(get_dashboard_summary_workflow)
    ],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
):
    return await dashboard_workflow.get_summary(workspace_id)


@router.get("/briefing", response_model=BriefingResponse)
async def get_briefing(
    briefing_workflow: Annotated[MorningBriefingWorkflow, Depends(get_morning_briefing_workflow)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    current_user: Annotated[dict, Depends(get_current_user)],
):
    return await briefing_workflow.get_briefing(workspace_id, current_user["id"])
