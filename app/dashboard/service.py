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
from app.spending.service import BudgetService, TransactionService
from app.todo.service import TodoService

logger = structlog.get_logger()


class DashboardService:
    def __init__(
        self,
        todo_service: TodoService,
        transaction_service: TransactionService,
        budget_service: BudgetService,
        investing_summary_service: InvestingSummaryService,
    ):
        self.todo_service = todo_service
        self.transaction_service = transaction_service
        self.budget_service = budget_service
        self.investing_summary_service = investing_summary_service

    async def get_summary(self, workspace_id: int) -> DashboardSummary:
        now = datetime.now(UTC)

        # 1. Fetch Todos
        todos_res = TodosSummary()
        try:
            open_count, overdue_count = await self.todo_service.get_summary_counts(
                workspace_id, now
            )
            next_due_items = await self.todo_service.get_next_due_items(workspace_id, now, limit=5)
            active_guardrail_todo_count = await self.todo_service.get_active_guardrail_todo_count(
                workspace_id
            )
            todos_res = TodosSummary(
                status="available",
                open_count=open_count,
                overdue_count=overdue_count,
                next_due_items=[
                    {
                        "public_id": str(todo.public_id),
                        "title": todo.title,
                        "due_date": todo.due_date.isoformat() if todo.due_date else None,
                        "priority": todo.priority,
                    }
                    for todo in next_due_items
                ],
                active_guardrail_todo_count=active_guardrail_todo_count,
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
            month_budget = await self.budget_service.get_month_total_budget(
                workspace_id=workspace_id,
                month_start=start_of_month.date(),
            )
            category_totals = await self.transaction_service.get_category_totals(
                workspace_id=workspace_id,
                from_date=start_of_month,
                to_date=now,
                type_filter=TransactionType.expense,
            )
            budgets, _ = await self.budget_service.list_budgets(
                workspace_id=workspace_id,
                month_start=start_of_month.date(),
                limit=5000,
                offset=0,
            )
            budget_amount_by_category = {budget.category_id: budget.amount for budget in budgets}
            top_overspent_categories = []
            for category_id, spent in category_totals.items():
                budget_amount = budget_amount_by_category.get(category_id)
                if not budget_amount or budget_amount <= 0:
                    continue
                overspend = spent - budget_amount
                if overspend <= 0:
                    continue
                ratio = spent / budget_amount
                top_overspent_categories.append({
                    "category_id": category_id,
                    "spent": spent,
                    "budget": budget_amount,
                    "overspend": overspend,
                    "ratio": ratio,
                })
            top_overspent_categories.sort(key=lambda item: item["overspend"], reverse=True)
            spending_res = SpendingSummary(
                status="available",
                month_spent=month_spent,
                month_budget=month_budget,
                top_overspent_categories=top_overspent_categories[:5],
            )
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
