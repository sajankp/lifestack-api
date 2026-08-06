import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from app.core.recurrence import MonthlyModeLiteral, OrdinalLiteral, validate_recurrence_fields
from app.imports.models import ImportModule
from app.spending.models import (
    KpiMetricType,
    KpiTargetDirection,
    KpiWindow,
    TransactionSourceType,
    TransactionType,
)

# Ledger entry kinds: regular spending transaction, or a capital transfer in/out
LedgerEntryKind = Literal["transaction", "transfer_out", "transfer_in"]

# ---------------------------------------------------------------------------
# Category Group schemas
# ---------------------------------------------------------------------------


class CategoryGroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    color: str | None = Field(default=None, max_length=20)
    icon: str | None = Field(default=None, max_length=50)


class CategoryGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    color: str | None = Field(default=None, max_length=20)
    icon: str | None = Field(default=None, max_length=50)


class CategoryGroupResponse(BaseModel):
    public_id: uuid.UUID
    name: str
    color: str | None
    icon: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CategoryMergeRequest(BaseModel):
    source_public_ids: list[uuid.UUID]


# ---------------------------------------------------------------------------
# Spending tag schemas
# ---------------------------------------------------------------------------


class TagCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    color: str | None = Field(default=None, max_length=20)


class TagUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    color: str | None = Field(default=None, max_length=20)


class TagResponse(BaseModel):
    public_id: uuid.UUID
    name: str
    color: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Category schemas
# ---------------------------------------------------------------------------


class CategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    color: str | None = Field(default=None, max_length=20)
    icon: str | None = Field(default=None, max_length=50)


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    color: str | None = Field(default=None, max_length=20)
    icon: str | None = Field(default=None, max_length=50)
    category_group_id: uuid.UUID | None = None


class CategoryResponse(BaseModel):
    public_id: uuid.UUID
    name: str
    is_system: bool
    color: str | None
    icon: str | None
    category_group_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Transaction schemas
# ---------------------------------------------------------------------------


class TransactionCreate(BaseModel):
    category_id: uuid.UUID  # public_id of the category
    account_id: uuid.UUID | None = None  # public_id of finance account/wallet
    amount: Decimal = Field(..., gt=0, decimal_places=2)
    type: TransactionType
    occurred_at: datetime
    description: str | None = Field(default=None, max_length=500)
    wallet_name: str | None = Field(default=None, max_length=120)
    labels: str | None = Field(default=None, max_length=500)
    tag_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)

    @field_validator("amount")
    @classmethod
    def validate_scale(cls, v: Decimal) -> Decimal:
        # Enforce exactly 2 dp precision
        if v != v.quantize(Decimal("0.01")):
            raise ValueError("Amount must have at most 2 decimal places")
        return v


class TransactionUpdate(BaseModel):
    category_id: uuid.UUID | None = None  # public_id of the category
    account_id: uuid.UUID | None = None  # public_id of finance account/wallet
    amount: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    type: TransactionType | None = None
    occurred_at: datetime | None = None
    description: str | None = Field(default=None, max_length=500)
    wallet_name: str | None = Field(default=None, max_length=120)
    labels: str | None = Field(default=None, max_length=500)
    tag_ids: list[uuid.UUID] | None = Field(default=None, max_length=20)

    @field_validator("amount")
    @classmethod
    def validate_scale(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v != v.quantize(Decimal("0.01")):
            raise ValueError("Amount must have at most 2 decimal places")
        return v


class SourceMetadataResponse(BaseModel):
    source_type: TransactionSourceType
    source_ref: str | None = None
    origin: Literal[
        "manual_entry",
        "bulk_import",
        "external_sync",
        "assistant_action",
        "document_extraction",
    ]
    label: str
    import_public_id: uuid.UUID | None = None
    import_module: ImportModule | None = None
    import_row_number: int | None = None
    rollback_supported: bool = False

    model_config = ConfigDict(use_enum_values=True)


class TransactionResponse(BaseModel):
    public_id: uuid.UUID
    category_id: uuid.UUID  # exposed as public_id
    account_id: uuid.UUID | None
    amount: Decimal
    type: TransactionType
    occurred_at: datetime
    description: str | None
    wallet_name: str | None
    labels: str | None
    tags: list[TagResponse] = Field(default_factory=list)
    source_type: TransactionSourceType
    source_ref: str | None
    source_metadata: SourceMetadataResponse
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
        use_enum_values=True,
        json_encoders={Decimal: str},
    )


class CategorySpendTotal(BaseModel):
    category_id: uuid.UUID
    total: Decimal

    model_config = ConfigDict(json_encoders={Decimal: str})


class TransactionSummaryResponse(BaseModel):
    income_total: Decimal = Decimal("0")
    expense_total: Decimal = Decimal("0")
    net_total: Decimal = Decimal("0")
    category_totals: list[CategorySpendTotal] = Field(default_factory=list)

    model_config = ConfigDict(json_encoders={Decimal: str})


# ---------------------------------------------------------------------------
# Budget schemas
# ---------------------------------------------------------------------------


class BudgetCreate(BaseModel):
    category_id: uuid.UUID | None = None
    category_group_id: uuid.UUID | None = None
    amount: Decimal = Field(..., gt=0, decimal_places=2)
    start_month: date
    end_month: date | None = None

    @field_validator("amount")
    @classmethod
    def validate_scale(cls, v: Decimal) -> Decimal:
        if v != v.quantize(Decimal("0.01")):
            raise ValueError("Amount must have at most 2 decimal places")
        return v

    @field_validator("start_month", "end_month")
    @classmethod
    def validate_first_of_month(cls, v: date | None) -> date | None:
        if v is not None and v.day != 1:
            raise ValueError("Month dates must be the first day of the month")
        return v

    @model_validator(mode="after")
    def validate_scope_and_dates(self) -> "BudgetCreate":
        if (self.category_id is None) == (self.category_group_id is None):
            raise ValueError("Exactly one of category_id or category_group_id must be set")
        if self.end_month is not None and self.end_month < self.start_month:
            raise ValueError("end_month must be greater than or equal to start_month")
        return self


class BudgetUpdate(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    end_month: date | None = None

    @field_validator("amount")
    @classmethod
    def validate_scale(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v != v.quantize(Decimal("0.01")):
            raise ValueError("Amount must have at most 2 decimal places")
        return v

    @field_validator("end_month")
    @classmethod
    def validate_first_of_month(cls, v: date | None) -> date | None:
        if v is not None and v.day != 1:
            raise ValueError("end_month must be the first day of the month")
        return v


class BudgetResponse(BaseModel):
    public_id: uuid.UUID
    category_id: uuid.UUID | None = None
    category_group_id: uuid.UUID | None = None
    amount: Decimal
    start_month: date
    end_month: date | None = None
    source_metadata: SourceMetadataResponse | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})


class BudgetChangeAmountRequest(BaseModel):
    amount: Decimal = Field(..., gt=0, decimal_places=2)
    from_month: date

    @field_validator("amount")
    @classmethod
    def validate_scale(cls, v: Decimal) -> Decimal:
        if v != v.quantize(Decimal("0.01")):
            raise ValueError("Amount must have at most 2 decimal places")
        return v

    @field_validator("from_month")
    @classmethod
    def validate_first_of_month(cls, v: date) -> date:
        if v.day != 1:
            raise ValueError("from_month must be the first day of the month")
        return v


class SpendingTrendPoint(BaseModel):
    month: str
    total_income: Decimal
    total_expense: Decimal
    net: Decimal
    transaction_count: int

    model_config = ConfigDict(json_encoders={Decimal: str})


class SpendingTrendResponse(BaseModel):
    from_month: str = Field(serialization_alias="from")
    to_month: str = Field(serialization_alias="to")
    months: list[SpendingTrendPoint]

    model_config = ConfigDict(populate_by_name=True, json_encoders={Decimal: str})


class RecurringTransactionCreate(BaseModel):
    category_id: uuid.UUID
    account_id: uuid.UUID | None = None
    amount: Decimal = Field(..., gt=0, decimal_places=2)
    type: TransactionType
    description: str | None = Field(default=None, max_length=500)
    frequency: Literal["daily", "weekly", "monthly", "yearly"] = Field(default="monthly")
    interval: int = Field(default=1, ge=1)
    anchor_date: date
    end_date: date | None = None
    monthly_mode: MonthlyModeLiteral = Field(default="day_of_month")
    by_weekday: int | None = Field(default=None, ge=0, le=6)
    by_ordinal: OrdinalLiteral | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_monthly_mode(self) -> "RecurringTransactionCreate":
        validate_recurrence_fields(
            self.frequency, self.monthly_mode, self.by_weekday, self.by_ordinal
        )
        return self


class RecurringTransactionUpdate(BaseModel):
    account_id: uuid.UUID | None = None
    amount: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    description: str | None = Field(default=None, max_length=500)
    frequency: Literal["daily", "weekly", "monthly", "yearly"] | None = None
    interval: int | None = Field(default=None, ge=1)
    end_date: date | None = None
    is_active: bool | None = None
    monthly_mode: MonthlyModeLiteral | None = Field(default=None)
    by_weekday: int | None = Field(default=None, ge=0, le=6)
    by_ordinal: OrdinalLiteral | None = Field(default=None)


class RecurringTransactionResponse(BaseModel):
    public_id: uuid.UUID
    category_id: uuid.UUID
    account_id: uuid.UUID | None
    amount: Decimal
    type: TransactionType
    description: str | None
    frequency: str
    interval: int
    anchor_date: date
    next_due_date: date
    end_date: date | None
    is_active: bool
    last_generated_at: datetime | None
    monthly_mode: str
    by_weekday: int | None
    by_ordinal: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})


class UpcomingTransactionItem(BaseModel):
    """A projected (not-yet-generated) transaction from a recurring rule."""

    recurring_public_id: uuid.UUID
    category_id: uuid.UUID
    account_id: uuid.UUID | None
    amount: Decimal
    type: TransactionType
    description: str | None
    projected_date: date
    frequency: str
    interval: int

    model_config = ConfigDict(json_encoders={Decimal: str})


class UpcomingPreviewResponse(BaseModel):
    days: int
    from_date: date
    to_date: date
    items: list[UpcomingTransactionItem]

    model_config = ConfigDict(json_encoders={Decimal: str})


# ---------------------------------------------------------------------------
# Spending Analytics schemas
# ---------------------------------------------------------------------------


class CategoryBreakdownItem(BaseModel):
    category_id: uuid.UUID
    category_name: str
    amount: Decimal
    pct_of_total: float
    transaction_count: int

    model_config = ConfigDict(json_encoders={Decimal: str})


class CategoryBreakdownOther(BaseModel):
    amount: Decimal
    pct_of_total: float
    category_count: int

    model_config = ConfigDict(json_encoders={Decimal: str})


class CategoryBreakdownResponse(BaseModel):
    from_date: date = Field(serialization_alias="from")
    to_date: date = Field(serialization_alias="to")
    type: TransactionType
    total: Decimal
    categories: list[CategoryBreakdownItem]
    other: CategoryBreakdownOther | None = None

    model_config = ConfigDict(populate_by_name=True, json_encoders={Decimal: str})


class TagBreakdownItem(BaseModel):
    tag_id: uuid.UUID
    tag_name: str
    amount: Decimal
    pct_of_total: float
    transaction_count: int

    model_config = ConfigDict(json_encoders={Decimal: str})


class TagBreakdownResponse(BaseModel):
    from_date: date = Field(serialization_alias="from")
    to_date: date = Field(serialization_alias="to")
    type: TransactionType
    total: Decimal
    tags: list[TagBreakdownItem]

    model_config = ConfigDict(populate_by_name=True, json_encoders={Decimal: str})


class BudgetPerformanceItem(BaseModel):
    category_id: uuid.UUID | None = None
    category_name: str | None = None
    category_group_id: uuid.UUID | None = None
    category_group_name: str | None = None
    budget_amount: Decimal | None
    actual_amount: Decimal
    utilization_pct: float | None
    remaining: Decimal | None
    status: Literal["on_track", "warning", "exceeded"]

    model_config = ConfigDict(use_enum_values=True, json_encoders={Decimal: str})


class BudgetSpotlightItem(BaseModel):
    category_group_id: uuid.UUID
    category_group_name: str
    budget_amount: Decimal
    actual_amount: Decimal
    utilization_pct: float
    remaining: Decimal
    status: Literal["on_track", "warning", "exceeded"]
    daily_amount_left: Decimal

    model_config = ConfigDict(use_enum_values=True, json_encoders={Decimal: str})


class BudgetPerformanceTotals(BaseModel):
    total_budgeted: Decimal
    total_actual: Decimal
    overall_utilization_pct: float | None

    model_config = ConfigDict(json_encoders={Decimal: str})


class BudgetPerformanceResponse(BaseModel):
    from_month: str = Field(serialization_alias="from")
    to_month: str = Field(serialization_alias="to")
    categories: list[BudgetPerformanceItem]
    totals: BudgetPerformanceTotals
    groups: list[BudgetPerformanceItem] = Field(default_factory=list)
    group_totals: BudgetPerformanceTotals

    model_config = ConfigDict(populate_by_name=True, json_encoders={Decimal: str})


class SavingsRatePoint(BaseModel):
    month: str
    income: Decimal
    expense: Decimal
    savings: Decimal
    savings_rate_pct: float | None

    model_config = ConfigDict(json_encoders={Decimal: str})


class SavingsRateTotals(BaseModel):
    total_income: Decimal
    total_expense: Decimal
    total_savings: Decimal
    average_savings_rate_pct: float | None

    model_config = ConfigDict(json_encoders={Decimal: str})


class SavingsRateResponse(BaseModel):
    from_month: str = Field(serialization_alias="from")
    to_month: str = Field(serialization_alias="to")
    months: list[SavingsRatePoint]
    period_totals: SavingsRateTotals

    model_config = ConfigDict(populate_by_name=True, json_encoders={Decimal: str})


# ---------------------------------------------------------------------------
# Transaction Ledger schemas
# ---------------------------------------------------------------------------


class LedgerEntry(BaseModel):
    """A single ledger entry (spending transaction or capital transfer) with a cumulative running balance.

    entry_kind discriminates between regular transactions and transfer events:
    - ``transaction``: a spending income/expense transaction
    - ``transfer_out``: a capital transfer leaving this account
    - ``transfer_in``: a capital transfer arriving at this account

    Transfer entries have ``category_id = None`` and ``wallet_name = None``.
    """

    public_id: uuid.UUID
    entry_kind: LedgerEntryKind  # discriminator between transaction and transfer rows
    category_id: uuid.UUID | None  # None for transfer entries
    account_id: uuid.UUID | None
    amount: Decimal
    type: TransactionType | None  # None for transfer entries (use entry_kind instead)
    occurred_at: datetime
    description: str | None
    wallet_name: str | None
    labels: str | None
    source_type: str
    running_balance: Decimal  # cumulative balance AFTER this entry
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})


class KpiCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    metric_type: KpiMetricType
    evaluation_window: KpiWindow
    category_id: uuid.UUID | None = None
    category_group_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    # gte=0, not gt=0: a net_cash_flow target of exactly 0 ("don't spend more
    # than you earn") is a valid v1 scenario (spend_total/income_total targets
    # are naturally non-negative anyway).
    target_value: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    target_direction: KpiTargetDirection | None = None
    display_format: Literal["amount", "percent"] = "amount"

    @model_validator(mode="after")
    def validate_filter_and_target(self) -> "KpiCreate":
        filters_set = sum(
            f is not None for f in (self.category_id, self.category_group_id, self.account_id)
        )
        if filters_set > 1:
            raise ValueError("At most one of category_id, category_group_id, account_id may be set")
        if (self.target_value is None) != (self.target_direction is None):
            raise ValueError("target_value and target_direction must be set together")
        return self


class KpiUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    category_id: uuid.UUID | None = None
    category_group_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    # gte=0, not gt=0: a net_cash_flow target of exactly 0 ("don't spend more
    # than you earn") is a valid v1 scenario (spend_total/income_total targets
    # are naturally non-negative anyway).
    target_value: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    target_direction: KpiTargetDirection | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def validate_filter_and_target(self) -> "KpiUpdate":
        filters_set = sum(
            f is not None for f in (self.category_id, self.category_group_id, self.account_id)
        )
        if filters_set > 1:
            raise ValueError("At most one of category_id, category_group_id, account_id may be set")
        if (self.target_value is None) != (self.target_direction is None):
            raise ValueError("target_value and target_direction must be set together")
        return self


class KpiResponse(BaseModel):
    public_id: uuid.UUID
    name: str
    metric_type: KpiMetricType
    evaluation_window: KpiWindow
    category_id: uuid.UUID | None = None
    category_group_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    currency_code: str
    target_value: Decimal | None = None
    target_direction: KpiTargetDirection | None = None
    display_format: str
    is_active: bool
    current_value: Decimal
    is_breached: bool
    window_start: date
    window_end: date
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, json_encoders={Decimal: str})


class LedgerResponse(BaseModel):
    account_public_id: uuid.UUID
    account_name: str
    account_currency: str
    opening_balance: Decimal  # balance before the first item in this page
    closing_balance: Decimal  # balance after the last item in this page
    total_entries: int  # total count of all entries (transactions + transfers)
    items: list[LedgerEntry]

    @computed_field
    @property
    def total_transactions(self) -> int:
        """Deprecated: use total_entries instead."""
        return self.total_entries

    model_config = ConfigDict(json_encoders={Decimal: str})
