from datetime import UTC, datetime

import structlog

from app.dashboard.schemas import (
    DashboardSummary,
    InvestingSummary,
    SpendingSummary,
    SystemSummary,
    TodosSummary,
)
from app.investing.service import InvestingSummaryService
from app.spending.models import TransactionType
from app.spending.service import TransactionService
from app.todo.service import TodoService

logger = structlog.get_logger()


class DashboardService:
    def __init__(
        self,
        todo_service: TodoService,
        transaction_service: TransactionService,
        investing_summary_service: InvestingSummaryService,
    ):
        self.todo_service = todo_service
        self.transaction_service = transaction_service
        self.investing_summary_service = investing_summary_service

    async def get_summary(self, workspace_id: int) -> DashboardSummary:
        now = datetime.now(UTC)

        # 1. Fetch Todos
        todos_res = TodosSummary()
        try:
            open_count, overdue_count = await self.todo_service.get_summary_counts(
                workspace_id, now
            )
            todos_res = TodosSummary(
                status="available", open_count=open_count, overdue_count=overdue_count
            )
        except Exception:
            logger.exception("dashboard_todos_fetch_failed", workspace_id=workspace_id)
            todos_res = TodosSummary(status="unavailable")

        # 2. Fetch Spending (current month)
        spending_res = SpendingSummary()
        try:
            start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            month_spent = await self.transaction_service.get_sum_by_type(
                workspace_id=workspace_id,
                type_filter=TransactionType.expense,
                from_date=start_of_month,
                to_date=now,
            )
            spending_res = SpendingSummary(status="available", month_spent=month_spent)
        except Exception:
            logger.exception("dashboard_spending_fetch_failed", workspace_id=workspace_id)
            spending_res = SpendingSummary(status="unavailable")

        # 3. Investing
        investing_res = InvestingSummary()
        try:
            investing_summary = await self.investing_summary_service.get_summary(workspace_id)
            investing_res = InvestingSummary(
                status="available",
                portfolio_value=investing_summary.portfolio_value,
                daily_change=investing_summary.daily_change,
                holdings_count=investing_summary.holdings_count,
            )
        except Exception:
            logger.exception("dashboard_investing_fetch_failed", workspace_id=workspace_id)
            investing_res = InvestingSummary(status="unavailable")

        return DashboardSummary(
            todos=todos_res,
            spending=spending_res,
            investing=investing_res,
            system=SystemSummary(generated_at=now),
        )
