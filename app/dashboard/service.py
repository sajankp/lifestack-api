from datetime import UTC, datetime

import structlog

from app.dashboard.schemas import (
    DashboardSummary,
    InvestingSummary,
    SpendingSummary,
    SystemSummary,
    TodosSummary,
)
from app.spending.models import TransactionType
from app.spending.service import TransactionService
from app.todo.service import TodoService

logger = structlog.get_logger()


class DashboardService:
    def __init__(self, todo_service: TodoService, transaction_service: TransactionService):
        self.todo_service = todo_service
        self.transaction_service = transaction_service

    async def get_summary(self, workspace_id: int) -> DashboardSummary:
        now = datetime.now(UTC)

        # 1. Fetch Todos
        open_count, overdue_count = await self.todo_service.get_summary_counts(workspace_id, now)

        # 2. Fetch Spending (current month)
        month_spent = 0.0
        try:
            start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            month_spent = await self.transaction_service.get_sum_by_type(
                workspace_id=workspace_id,
                type_filter=TransactionType.expense,
                from_date=start_of_month,
                to_date=now,
            )
        except Exception as e:
            logger.error("dashboard_spending_fetch_failed", error=str(e), workspace_id=workspace_id)

        # 3. Investing (stubbed for V1)

        return DashboardSummary(
            todos=TodosSummary(
                open_count=open_count,
                overdue_count=overdue_count,
            ),
            spending=SpendingSummary(
                month_spent=month_spent,
            ),
            investing=InvestingSummary(),
            system=SystemSummary(generated_at=now),
        )
