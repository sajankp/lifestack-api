import uuid
from datetime import UTC, datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class AccountType(StrEnum):
    bank = "bank"
    brokerage = "brokerage"
    wallet = "wallet"


class TransferModule(StrEnum):
    spending = "spending"
    investing = "investing"


class Currency(SQLModel, table=True):
    __tablename__ = "currencies"

    code: str = Field(primary_key=True, max_length=10)
    name: str = Field(max_length=64)
    symbol: str | None = Field(default=None, max_length=8)
    minor_unit: int = Field(default=2)
    is_active: bool = Field(default=True)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )


class WorkspaceCurrency(SQLModel, table=True):
    __tablename__ = "workspace_currencies"

    id: int | None = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    currency_code: str = Field(foreign_key="currencies.code", max_length=10, index=True)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id",
            "currency_code",
            name="uq_workspace_currency",
        ),
    )


class Account(SQLModel, table=True):
    __tablename__ = "accounts"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, unique=True, index=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    name: str = Field(max_length=100)
    account_type: AccountType = Field(default=AccountType.brokerage, sa_type=sa.String())
    default_currency_code: str = Field(foreign_key="currencies.code", max_length=10, index=True)
    is_active: bool = Field(default=True)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "name", name="uq_account_workspace_name"),
    )


class WorkspaceFinanceSetting(SQLModel, table=True):
    __tablename__ = "workspace_finance_settings"

    id: int | None = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspaces.id", unique=True, index=True)
    reporting_currency_code: str | None = Field(
        default=None, foreign_key="currencies.code", max_length=10
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )


class FxRate(SQLModel, table=True):
    __tablename__ = "fx_rates"

    id: int | None = Field(default=None, primary_key=True)
    base_currency_code: str = Field(foreign_key="currencies.code", max_length=10, index=True)
    quote_currency_code: str = Field(foreign_key="currencies.code", max_length=10, index=True)
    rate: float = Field(sa_type=sa.Numeric(precision=20, scale=10))
    as_of: datetime = Field(sa_type=sa.DateTime(timezone=True), index=True)
    fetched_at: datetime = Field(sa_type=sa.DateTime(timezone=True), index=True)
    source: str = Field(max_length=64, index=True)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "base_currency_code",
            "quote_currency_code",
            "as_of",
            "source",
            name="uq_fx_rate_pair_asof_source",
        ),
    )


class CapitalTransfer(SQLModel, table=True):
    __tablename__ = "capital_transfers"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, unique=True, index=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    actor_id: int = Field(foreign_key="users.id", index=True)

    from_module: TransferModule = Field(sa_type=sa.String(), default=TransferModule.spending)
    to_module: TransferModule = Field(sa_type=sa.String(), default=TransferModule.investing)

    from_account_id: int = Field(foreign_key="accounts.id", index=True)
    to_account_id: int = Field(foreign_key="accounts.id", index=True)
    from_currency_code: str = Field(foreign_key="currencies.code", max_length=10, index=True)
    to_currency_code: str = Field(foreign_key="currencies.code", max_length=10, index=True)

    gross_amount: float = Field(sa_type=sa.Numeric(precision=14, scale=2))
    fx_rate_used: float | None = Field(default=None, sa_type=sa.Numeric(precision=20, scale=10))
    fx_fee_amount: float = Field(default=0, sa_type=sa.Numeric(precision=14, scale=2))
    platform_fee_amount: float = Field(default=0, sa_type=sa.Numeric(precision=14, scale=2))
    tax_amount: float = Field(default=0, sa_type=sa.Numeric(precision=14, scale=2))
    net_amount_received: float = Field(sa_type=sa.Numeric(precision=14, scale=2))
    occurred_at: datetime = Field(sa_type=sa.DateTime(timezone=True), index=True)
    notes: str | None = Field(default=None, max_length=500)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
