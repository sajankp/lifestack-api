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


class OrderType(StrEnum):
    buy = "buy"
    sell = "sell"


class CorporateActionType(StrEnum):
    split = "split"
    bonus = "bonus"


class Company(SQLModel, table=True):
    __tablename__ = "investing_companies"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int | None = Field(
        default=None, foreign_key="workspaces.id", index=True, nullable=True
    )
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
        sa.Index(
            "uq_global_company_name",
            "name",
            unique=True,
            postgresql_where=sa.text("workspace_id IS NULL"),
        ),
        sa.Index(
            "uq_workspace_company_name",
            "workspace_id",
            "name",
            unique=True,
            postgresql_where=sa.text("workspace_id IS NOT NULL"),
        ),
    )


class Instrument(SQLModel, table=True):
    __tablename__ = "investing_instruments"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int | None = Field(
        default=None, foreign_key="workspaces.id", index=True, nullable=True
    )
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
        sa.Index(
            "uq_global_instrument_symbol",
            "symbol",
            unique=True,
            postgresql_where=sa.text("workspace_id IS NULL"),
        ),
        sa.Index(
            "uq_workspace_instrument_symbol",
            "workspace_id",
            "symbol",
            unique=True,
            postgresql_where=sa.text("workspace_id IS NOT NULL"),
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
    # 6 dp to match OrderLot.cost_per_unit and the FIFO replay's avg_cost
    # output; storing 2 dp truncated low-NAV/high-qty cost basis (spec-046).
    avg_cost: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=6))
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
    trigger_type: str | None = Field(default=None, sa_type=sa.String(length=20), nullable=True)
    trigger_ref: uuid.UUID | None = Field(default=None, sa_type=sa.Uuid, nullable=True)

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
    holding_id: int = Field(index=True)
    price_date: date = Field(sa_type=sa.Date())
    unit_price: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=6))
    source: str = Field(default="manual", max_length=16)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["holding_id"],
            ["investing_holdings.id"],
            name="fk_holding_prices_holding",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("holding_id", "price_date", name="uq_holding_price_day"),
    )


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


class InvestingOrder(SQLModel, table=True):
    __tablename__ = "investing_orders"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    account_id: int = Field(index=True)
    order_type: str = Field(
        sa_column=sa.Column(
            sa.Enum("buy", "sell", name="investing_order_type"),
            nullable=False,
        )
    )
    symbol: str = Field(max_length=20)
    instrument_id: int | None = Field(
        default=None, foreign_key="investing_instruments.id", index=True, nullable=True
    )

    quantity: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=8))
    price_per_unit: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=6))
    gross_amount: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=2))
    brokerage_fee: Decimal = Field(default=Decimal("0"), sa_type=sa.Numeric(precision=12, scale=2))
    tax_amount: Decimal = Field(default=Decimal("0"), sa_type=sa.Numeric(precision=12, scale=2))
    other_fees: Decimal = Field(default=Decimal("0"), sa_type=sa.Numeric(precision=12, scale=2))
    net_amount: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=2))
    currency: str = Field(max_length=10)
    exchange_name: str | None = Field(default=None, max_length=50, nullable=True)

    occurred_at: datetime = Field(sa_type=sa.DateTime(timezone=True))
    notes: str | None = Field(default=None, nullable=True)

    realized_gain_loss: Decimal | None = Field(
        default=None, sa_type=sa.Numeric(precision=18, scale=2), nullable=True
    )
    avg_cost_at_sale: Decimal | None = Field(
        default=None, sa_type=sa.Numeric(precision=18, scale=6), nullable=True
    )

    source_type: str = Field(default="manual", sa_type=sa.String(length=32), index=True)
    source_ref: str | None = Field(default=None, max_length=255, nullable=True)
    source_import_id: int | None = Field(
        default=None, foreign_key="import_batches.id", index=True, nullable=True
    )

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
            name="fk_investing_orders_account_workspace",
        ),
        sa.Index(
            "ix_investing_orders_workspace_symbol_account", "workspace_id", "symbol", "account_id"
        ),
        sa.Index("ix_investing_orders_workspace_occurred_at", "workspace_id", "occurred_at"),
        sa.Index("ix_investing_orders_workspace_import", "workspace_id", "source_import_id"),
    )


class OrderLot(SQLModel, table=True):
    """A FIFO cost-basis lot created by a single buy order, or by a bonus
    corporate action (spec-051).

    One row per buy order (or bonus issue) on a holding. ``remaining_quantity``
    is fully recomputed (deleted and recreated) every time the holding's order
    history is replayed — see ``InvestingOrderService._recompute_holding``.
    Exactly one of ``buy_order_id``/``corporate_action_id`` is set: a split
    scales an existing buy-derived lot in place (no new lot), while a bonus
    issue creates a new lot with no originating buy order.
    """

    __tablename__ = "investing_order_lots"

    id: int | None = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    holding_id: int = Field(index=True)
    buy_order_id: int | None = Field(default=None, index=True, nullable=True)
    corporate_action_id: int | None = Field(default=None, index=True, nullable=True)

    original_quantity: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=8))
    remaining_quantity: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=8))
    cost_per_unit: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=6))
    acquired_at: datetime = Field(sa_type=sa.DateTime(timezone=True))

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["holding_id"],
            ["investing_holdings.id"],
            name="fk_investing_order_lots_holding",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["buy_order_id"],
            ["investing_orders.id"],
            name="fk_investing_order_lots_buy_order",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["corporate_action_id"],
            ["investing_corporate_actions.id"],
            name="fk_investing_order_lots_corporate_action",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "(buy_order_id IS NOT NULL AND corporate_action_id IS NULL) OR "
            "(buy_order_id IS NULL AND corporate_action_id IS NOT NULL)",
            name="ck_investing_order_lots_exactly_one_origin",
        ),
        sa.Index("ix_investing_order_lots_holding_acquired_at", "holding_id", "acquired_at"),
    )


class CorporateAction(SQLModel, table=True):
    """A stock split, reverse split, or bonus issue (spec-051).

    Replayed inside ``InvestingOrderService._replay_orders`` alongside buy/
    sell orders, ordered by ``ex_date``. A split/reverse-split scales every
    open ``OrderLot`` in place; a bonus issue creates a new zero-cost lot.
    Never touches ``investing_cash_balances`` — cash-neutral by construction.
    """

    __tablename__ = "investing_corporate_actions"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    account_id: int = Field(index=True)
    symbol: str = Field(max_length=20)
    action_type: str = Field(
        sa_column=sa.Column(
            sa.Enum("split", "bonus", name="investing_corporate_action_type"),
            nullable=False,
        )
    )
    # Semantics are action_type-dependent (see spec-051): for a split,
    # ratio_base "old" units become ratio_quote "new" units (existing lots
    # scaled in place); for a bonus, ratio_quote units are granted free per
    # ratio_base units held (a new zero-cost lot is added).
    ratio_base: Decimal = Field(sa_type=sa.Numeric(precision=12, scale=4))
    ratio_quote: Decimal = Field(sa_type=sa.Numeric(precision=12, scale=4))
    ex_date: date = Field(sa_type=sa.Date())
    notes: str | None = Field(default=None, max_length=255, nullable=True)

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
            name="fk_investing_corporate_actions_account_workspace",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "account_id",
            "symbol",
            "ex_date",
            "action_type",
            name="uq_corporate_action_workspace_account_symbol_exdate_type",
        ),
        sa.Index(
            "ix_investing_corporate_actions_workspace_symbol_account",
            "workspace_id",
            "symbol",
            "account_id",
        ),
    )


class LotConsumption(SQLModel, table=True):
    """Records that a sell order consumed (part of) a FIFO lot.

    Audit trail of which lots a sell drew from; recreated alongside
    ``OrderLot`` rows on every replay.
    """

    __tablename__ = "investing_lot_consumptions"

    id: int | None = Field(default=None, primary_key=True)
    sell_order_id: int = Field(index=True)
    lot_id: int = Field(index=True)

    quantity_consumed: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=8))
    cost_per_unit: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=6))

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["sell_order_id"],
            ["investing_orders.id"],
            name="fk_investing_lot_consumptions_sell_order",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lot_id"],
            ["investing_order_lots.id"],
            name="fk_investing_lot_consumptions_lot",
            ondelete="CASCADE",
        ),
    )


class HoldingVerification(SQLModel, table=True):
    """A depository-vs-Lifestack holdings comparison snapshot (spec-060).

    Written once per Demat CAS import commit. Never writes/reads
    ``Holding``, ``InvestingOrder``, ``OrderLot``, or cash rows — this is a
    read-only verification record, not an ingestion path (a Demat CAS has no
    price, so it cannot safely become an order). ``report_json`` holds the
    full per-ISIN comparison; the four `*_count` columns are a fast summary.
    """

    __tablename__ = "investing_holding_verifications"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    account_id: int = Field(index=True)
    source_import_id: int = Field(foreign_key="import_batches.id", index=True)

    # Plain string, not enum — only NSDL is implemented (spec-060), but the
    # column shouldn't need a migration when CDSL is added later.
    source: str = Field(max_length=32)
    statement_date: date | None = Field(default=None, sa_type=sa.Date())

    match_count: int = Field(default=0)
    quantity_drift_count: int = Field(default=0)
    missing_in_lifestack_count: int = Field(default=0)
    missing_at_depository_count: int = Field(default=0)

    report_json: list = Field(default_factory=list, sa_type=sa.JSON)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["account_id", "workspace_id"],
            ["accounts.id", "accounts.workspace_id"],
            name="fk_investing_holding_verifications_account_workspace",
        ),
    )
