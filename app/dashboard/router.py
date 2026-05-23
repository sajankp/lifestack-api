from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.dependencies import (
    get_current_workspace_id,
    get_investing_summary_service,
    get_spending_transaction_service,
    get_todo_service,
)
from app.dashboard.schemas import DashboardSummary
from app.dashboard.service import DashboardService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


async def get_dashboard_service(
    todo_service=Depends(get_todo_service),
    transaction_service=Depends(get_spending_transaction_service),
    investing_summary_service=Depends(get_investing_summary_service),
) -> DashboardService:
    return DashboardService(todo_service, transaction_service, investing_summary_service)


@router.get("/summary", response_model=DashboardSummary)
async def get_summary(
    dashboard_service: Annotated[DashboardService, Depends(get_dashboard_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
):
    return await dashboard_service.get_summary(workspace_id)
