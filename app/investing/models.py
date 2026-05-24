import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class InstrumentType(StrEnum):
    stock = "stock"
    etf = "etf"
    mutual_fund = "mutual_fund"


class Company(SQLModel, table=True):
    __tablename__ = "investing_companies"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    name: str = Field(max_length=255)
    ticker: str | None = Field(default=None, max_length=20, index=True)
    isin: str | None = Field(default=None, max_length=20)
    sector: str | None = Field(default=None, max_length=100)
    country_code: str | None = Field(default=None, max_length=10)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id",
            "name",
            name="uq_investing_company_workspace_name",
        ),
    )


class Instrument(SQLModel, table=True):
    __tablename__ = "investing_instruments"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    symbol: str = Field(max_length=20)
    name: str = Field(max_length=255)
    instrument_type: str = Field(
        default="stock",
        sa_column=sa.Column(
            sa.Enum("stock", "etf", "mutual_fund", name="instrument_type"),
            nullable=False,
            server_default="stock",
        ),
    )
    isin: str | None = Field(default=None, max_length=20)
    exchange: str | None = Field(default=None, max_length=50)
    provider_key: str | None = Field(default=None, max_length=100)
    company_id: int | None = Field(default=None, foreign_key="investing_companies.id", index=True)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id",
            "symbol",
            name="uq_investing_instrument_workspace_symbol",
        ),
    )


class InstrumentConstituent(SQLModel, table=True):
    __tablename__ = "investing_instrument_constituents"

    id: int | None = Field(default=None, primary_key=True)
    instrument_id: int = Field(foreign_key="investing_instruments.id", index=True)
    constituent_company_id: int = Field(foreign_key="investing_companies.id", index=True)
    weight: Decimal = Field(sa_type=sa.Numeric(precision=10, scale=8))
    as_of_date: date = Field(sa_type=sa.Date())
    source: str = Field(max_length=64)
    fetched_at: datetime = Field(sa_type=sa.DateTime(timezone=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "instrument_id",
            "constituent_company_id",
            "as_of_date",
            "source",
            name="uq_investing_constituent_snapshot",
        ),
    )


class Holding(SQLModel, table=True):
    __tablename__ = "investing_holdings"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    instrument_id: int | None = Field(
        default=None, foreign_key="investing_instruments.id", index=True
    )

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
