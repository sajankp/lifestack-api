from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TodosSummary(BaseModel):
    open_count: int
    overdue_count: int
    next_due_items: list[dict] = []
    active_guardrail_todo_count: int = 0


class SpendingSummary(BaseModel):
    month_spent: float
    month_budget: float | None = None
    top_overspent_categories: list[dict] = []


class InvestingSummary(BaseModel):
    portfolio_value: float = 0.0
    daily_change: float | None = None
    holdings_count: int = 0


class SystemSummary(BaseModel):
    generated_at: datetime


class DashboardSummary(BaseModel):
    todos: TodosSummary
    spending: SpendingSummary
    investing: InvestingSummary
    system: SystemSummary

    model_config = ConfigDict(from_attributes=True)
