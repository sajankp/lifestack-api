import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class TransactionType(StrEnum):
    income = "income"
    expense = "expense"


class SpendingCategory(SQLModel, table=True):
    __tablename__ = "spending_categories"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)

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
    )


class SpendingTransaction(SQLModel, table=True):
    __tablename__ = "spending_transactions"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    category_id: int = Field(foreign_key="spending_categories.id", index=True)
    account_id: int | None = Field(default=None, foreign_key="accounts.id", index=True)
    recurring_transaction_id: int | None = Field(
        default=None, foreign_key="recurring_transactions.id", index=True
    )

    amount: Decimal = Field(sa_type=sa.Numeric(precision=12, scale=2))
    type: TransactionType = Field(sa_type=sa.String())
    occurred_at: datetime = Field(sa_type=sa.DateTime(timezone=True))
    description: str | None = Field(default=None, max_length=500)
    wallet_name: str | None = Field(default=None, max_length=120)
    labels: str | None = Field(default=None, max_length=500)

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
    )


class SpendingBudget(SQLModel, table=True):
    __tablename__ = "spending_budgets"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    category_id: int = Field(foreign_key="spending_categories.id", index=True)

    amount: Decimal = Field(sa_type=sa.Numeric(precision=12, scale=2))
    # First day of the budget month, e.g. 2026-03-01
    month_start: date = Field(sa_type=sa.Date())

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
        sa.UniqueConstraint(
            "workspace_id", "category_id", "month_start", name="uq_budget_workspace_category_month"
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
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
