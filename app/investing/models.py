import uuid
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class Holding(SQLModel, table=True):
    __tablename__ = "investing_holdings"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    symbol: str = Field(max_length=20)
    account_name: str = Field(default="primary", max_length=100)
    quantity: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=8))
    avg_cost: Decimal = Field(sa_type=sa.Numeric(precision=12, scale=2))
    currency: str = Field(max_length=10)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id", "symbol", "account_name", name="uq_holding_workspace_symbol_account"
        ),
    )


class CashBalance(SQLModel, table=True):
    __tablename__ = "investing_cash_balances"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    account_name: str = Field(max_length=100)
    balance: Decimal = Field(sa_type=sa.Numeric(precision=12, scale=2))
    currency: str = Field(max_length=10)
    as_of: datetime = Field(sa_type=sa.DateTime(timezone=True))

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
