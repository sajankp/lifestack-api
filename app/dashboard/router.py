from typing import Annotated

from fastapi import APIRouter, Depends

from app.application.workflows import DashboardSummaryWorkflow
from app.core.dependencies import (
    get_current_workspace_id,
    get_dashboard_summary_workflow,
)
from app.dashboard.schemas import DashboardSummary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary)
async def get_summary(
    dashboard_workflow: Annotated[
        DashboardSummaryWorkflow, Depends(get_dashboard_summary_workflow)
    ],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
):
    return await dashboard_workflow.get_summary(workspace_id)
