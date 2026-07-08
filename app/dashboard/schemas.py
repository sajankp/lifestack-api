from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.spending.schemas import BudgetSpotlightItem


class TodosSummary(BaseModel):
    status: str = "available"
    open_count: int = 0
    overdue_count: int = 0
    next_due_items: list[dict] = []
    active_guardrail_todo_count: int = 0


class SpendingSummary(BaseModel):
    status: str = "available"
    month_spent: Decimal = Decimal("0")
    budget_spotlight: list[BudgetSpotlightItem] = []
    top_overspent_categories: list[dict] = []


class InvestingSummary(BaseModel):
    status: str = "available"
    portfolio_value: Decimal | None = Decimal("0")
    invested_value: Decimal | None = Decimal("0")
    total_gain_loss: Decimal | None = Decimal("0")
    total_gain_loss_pct: Decimal | None = None
    daily_change: Decimal | None = None
    daily_change_pct: Decimal | None = None
    snapshot_date: date | None = None
    previous_snapshot_date: date | None = None
    valuation_status: str = "unavailable"
    holdings_count: int = 0
    cash_total: Decimal | None = Decimal("0")


class SystemSummary(BaseModel):
    generated_at: datetime


class DashboardSummary(BaseModel):
    todos: TodosSummary
    spending: SpendingSummary
    investing: InvestingSummary
    system: SystemSummary

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})


class BriefingSource(BaseModel):
    entity_type: str | None = None
    entity_public_id: str | None = None
    route: str


class BriefingLine(BaseModel):
    severity: Literal["critical", "warning", "info"]
    text: str
    source: BriefingSource


class BriefingResponse(BaseModel):
    generated_at: datetime
    all_clear: bool
    reporting_currency: str
    lines: list[BriefingLine] = []
