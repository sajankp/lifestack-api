import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

import sqlalchemy as sa
from pydantic import field_validator
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
    account_id: int = Field(index=True)
    quantity: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=8))
    avg_cost: Decimal = Field(sa_type=sa.Numeric(precision=12, scale=2))
    currency: str = Field(max_length=10)
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
        sa.UniqueConstraint(
            "workspace_id", "symbol", "account_id", name="uq_holding_workspace_symbol_account"
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "workspace_id"],
            ["accounts.id", "accounts.workspace_id"],
            name="fk_investing_holdings_account_workspace",
        ),
    )


class CashBalance(SQLModel, table=True):
    __tablename__ = "investing_cash_balances"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    account_id: int = Field(index=True)
    balance: Decimal = Field(sa_type=sa.Numeric(precision=12, scale=2))
    currency: str = Field(max_length=10)
    as_of: datetime = Field(sa_type=sa.DateTime(timezone=True))
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
            ["account_id", "workspace_id"],
            ["accounts.id", "accounts.workspace_id"],
            name="fk_investing_cash_balances_account_workspace",
        ),
    )


class HoldingPrice(SQLModel, table=True):
    __tablename__ = "holding_prices"
    id: int | None = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    holding_id: int = Field(foreign_key="investing_holdings.id", index=True)
    price_date: date = Field(sa_type=sa.Date())
    unit_price: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=6))
    source: str = Field(default="manual", max_length=16)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (sa.UniqueConstraint("holding_id", "price_date", name="uq_holding_price_day"),)


class PortfolioSnapshot(SQLModel, table=True):
    __tablename__ = "portfolio_snapshots"
    id: int | None = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    snapshot_date: date = Field(sa_type=sa.Date())
    total_value: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=2))
    total_cost: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=2))
    holdings_value: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=2))
    cash_value: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=2))
    currency_code: str = Field(max_length=10)
    fx_rates_used: dict = Field(default_factory=dict, sa_type=sa.JSON())
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    model_config = {
        "validate_default": True,
        "validate_assignment": True,
    }

    @field_validator("fx_rates_used")
    @classmethod
    def validate_fx_rates(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("fx_rates_used must be a dictionary")
        for key, val in v.items():
            if not isinstance(key, str) or not key.isupper() or len(key) != 3:
                raise ValueError(f"Invalid currency code key: {key}")
            try:
                numeric_val = Decimal(str(val))
                if numeric_val <= 0:
                    raise ValueError("FX rate must be positive")
            except Exception as e:
                raise ValueError(f"Invalid FX rate value for {key}: {val}") from e
        return v

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "snapshot_date", name="uq_snapshot_workspace_date"),
        sa.Index(
            "ix_portfolio_snapshots_workspace_snapshot_date_desc",
            "workspace_id",
            sa.text("snapshot_date DESC"),
        ),
    )
