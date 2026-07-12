import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

from app.core.recurrence import MonthlyRecurrenceMode


class TransactionType(StrEnum):
    income = "income"
    expense = "expense"


class TransactionSort(StrEnum):
    """Sort options for listing spending transactions.

    ``date_*`` sorts by the transaction date (``occurred_at``); ``amount_*``
    sorts by the transaction amount. ``created_at`` is always applied as a
    secondary key so pagination stays stable when the primary key ties.
    """

    date_desc = "date_desc"
    date_asc = "date_asc"
    amount_desc = "amount_desc"
    amount_asc = "amount_asc"


class TransactionSourceType(StrEnum):
    manual = "manual"
    imported = "imported"
    synced = "synced"
    assistant = "assistant"
    order = "order"


class KpiMetricType(StrEnum):
    """v1 metric types only — exactly these three (spec-077). Extending this
    enum (e.g. savings_rate, category_ratio) is a follow-up spec, not a
    silent addition here."""

    spend_total = "spend_total"
    income_total = "income_total"
    net_cash_flow = "net_cash_flow"


class KpiWindow(StrEnum):
    calendar_month = "calendar_month"
    calendar_week = "calendar_week"
    rolling_30d = "rolling_30d"


class KpiTargetDirection(StrEnum):
    lte = "lte"
    gte = "gte"


class CategoryGroup(SQLModel, table=True):
    __tablename__ = "category_groups"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)

    name: str = Field(max_length=100)
    normalized_name: str = Field(max_length=100)
    color: str | None = Field(default=None, max_length=20)
    icon: str | None = Field(default=None, max_length=50)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint("id", "workspace_id", name="uq_category_group_id_workspace"),
        sa.UniqueConstraint(
            "workspace_id", "normalized_name", name="uq_category_group_workspace_name"
        ),
    )


class SpendingCategory(SQLModel, table=True):
    __tablename__ = "spending_categories"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    category_group_id: int | None = Field(default=None, index=True)

    name: str = Field(max_length=100)
    # Stored normalised (lowercase, stripped) for uniqueness checks
    normalized_name: str = Field(max_length=100)
    is_system: bool = Field(default=False)
    color: str | None = Field(default=None, max_length=20)
    icon: str | None = Field(default=None, max_length=50)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint("id", "workspace_id", name="uq_category_id_workspace"),
        sa.UniqueConstraint("workspace_id", "normalized_name", name="uq_category_workspace_name"),
        sa.ForeignKeyConstraint(
            ["category_group_id", "workspace_id"],
            ["category_groups.id", "category_groups.workspace_id"],
            name="fk_spending_categories_group_workspace",
        ),
    )


class SpendingTransaction(SQLModel, table=True):
    __tablename__ = "spending_transactions"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    category_id: int = Field(foreign_key="spending_categories.id", index=True)
    account_id: int | None = Field(default=None, index=True)
    recurring_transaction_id: int | None = Field(
        default=None, foreign_key="recurring_transactions.id", index=True
    )

    amount: Decimal = Field(sa_type=sa.Numeric(precision=12, scale=2))
    type: TransactionType = Field(sa_type=sa.String())
    occurred_at: datetime = Field(sa_type=sa.DateTime(timezone=True))
    description: str | None = Field(default=None, max_length=500)
    wallet_name: str | None = Field(default=None, max_length=120)
    labels: str | None = Field(default=None, max_length=500)
    source_type: TransactionSourceType = Field(
        default=TransactionSourceType.manual, sa_type=sa.String(length=32), index=True
    )
    source_ref: str | None = Field(default=None, max_length=255)
    source_import_id: int | None = Field(default=None, foreign_key="import_batches.id", index=True)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["category_id", "workspace_id"],
            ["spending_categories.id", "spending_categories.workspace_id"],
            name="fk_spending_transactions_category_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "workspace_id"],
            ["accounts.id", "accounts.workspace_id"],
            name="fk_spending_transactions_account_workspace",
        ),
    )


class SpendingBudget(SQLModel, table=True):
    __tablename__ = "spending_budgets"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)

    category_id: int | None = Field(default=None, index=True)
    category_group_id: int | None = Field(default=None, index=True)

    amount: Decimal = Field(sa_type=sa.Numeric(precision=12, scale=2))
    start_month: date = Field(sa_type=sa.Date())
    end_month: date | None = Field(default=None, sa_type=sa.Date())

    source_type: str = Field(default="manual", sa_type=sa.String(length=32), index=True)
    source_ref: str | None = Field(default=None, max_length=255)
    source_import_id: int | None = Field(default=None, foreign_key="import_batches.id", index=True)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["category_id", "workspace_id"],
            ["spending_categories.id", "spending_categories.workspace_id"],
            name="fk_spending_budgets_category_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["category_group_id", "workspace_id"],
            ["category_groups.id", "category_groups.workspace_id"],
            name="fk_spending_budgets_group_workspace",
        ),
        sa.CheckConstraint(
            "(category_id IS NULL) != (category_group_id IS NULL)",
            name="ck_budget_scope",
        ),
    )


class RecurringTransaction(SQLModel, table=True):
    __tablename__ = "recurring_transactions"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    category_id: int = Field(foreign_key="spending_categories.id", index=True)
    amount: Decimal = Field(sa_type=sa.Numeric(precision=12, scale=2))
    type: TransactionType = Field(sa_type=sa.String())
    description: str | None = Field(default=None, max_length=500)
    frequency: str = Field(default="monthly", max_length=16)
    interval: int = Field(default=1, ge=1)
    anchor_date: date = Field(sa_type=sa.Date())
    next_due_date: date = Field(sa_type=sa.Date())
    end_date: date | None = Field(default=None, sa_type=sa.Date())
    is_active: bool = Field(default=True)
    last_generated_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))

    # Calendar recurrence modes (spec-053) — only meaningful when
    # frequency="monthly"; enforced by a DB CHECK, not just app validation.
    monthly_mode: str = Field(
        default=MonthlyRecurrenceMode.day_of_month.value,
        sa_column=sa.Column(
            sa.Enum(
                "day_of_month",
                "last_day",
                "nth_weekday",
                name="recurrence_monthly_mode",
                create_type=False,
            ),
            nullable=False,
            server_default="day_of_month",
        ),
    )
    by_weekday: int | None = Field(default=None, sa_type=sa.SmallInteger())
    by_ordinal: int | None = Field(default=None, sa_type=sa.SmallInteger())

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.CheckConstraint(
            "(monthly_mode = 'nth_weekday') = (by_weekday IS NOT NULL AND by_ordinal IS NOT NULL)",
            name="ck_recurring_transactions_nth_weekday_fields",
        ),
        sa.CheckConstraint(
            "by_weekday IS NULL OR by_weekday BETWEEN 0 AND 6",
            name="ck_recurring_transactions_by_weekday_range",
        ),
        sa.CheckConstraint(
            "by_ordinal IS NULL OR by_ordinal IN (-1, 1, 2, 3, 4)",
            name="ck_recurring_transactions_by_ordinal_range",
        ),
    )


class FinancialKpi(SQLModel, table=True):
    """Custom financial KPI definition (spec-077).

    Single-currency-per-KPI is NOT a DB constraint: the filter's resolved
    account set can change after creation (e.g. an account's currency edited
    elsewhere), so the constraint is re-checked by the service at evaluation
    time, not just enforced here at write time.
    """

    __tablename__ = "financial_kpis"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)

    name: str = Field(max_length=100)
    metric_type: KpiMetricType = Field(sa_type=sa.String(length=32))
    evaluation_window: KpiWindow = Field(sa_type=sa.String(length=20))

    category_id: int | None = Field(default=None, index=True)
    category_group_id: int | None = Field(default=None, index=True)
    account_id: int | None = Field(default=None, index=True)

    currency_code: str = Field(foreign_key="currencies.code", max_length=10)

    target_value: Decimal | None = Field(default=None, sa_type=sa.Numeric(precision=14, scale=2))
    target_direction: KpiTargetDirection | None = Field(default=None, sa_type=sa.String(length=4))
    display_format: str = Field(default="amount", max_length=20)
    is_active: bool = Field(default=True)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.CheckConstraint(
            "metric_type IN ('spend_total', 'income_total', 'net_cash_flow')",
            name="ck_financial_kpis_metric_type",
        ),
        sa.CheckConstraint(
            "evaluation_window IN ('calendar_month', 'calendar_week', 'rolling_30d')",
            name="ck_financial_kpis_window",
        ),
        sa.CheckConstraint(
            "target_direction IS NULL OR target_direction IN ('lte', 'gte')",
            name="ck_financial_kpis_target_direction",
        ),
        sa.CheckConstraint(
            "(target_value IS NULL) = (target_direction IS NULL)",
            name="ck_financial_kpis_target_pair",
        ),
        sa.ForeignKeyConstraint(
            ["category_id", "workspace_id"],
            ["spending_categories.id", "spending_categories.workspace_id"],
            name="fk_financial_kpis_category_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["category_group_id", "workspace_id"],
            ["category_groups.id", "category_groups.workspace_id"],
            name="fk_financial_kpis_group_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "workspace_id"],
            ["accounts.id", "accounts.workspace_id"],
            name="fk_financial_kpis_account_workspace",
        ),
    )
