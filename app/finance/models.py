import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

import sqlalchemy as sa
from pydantic import field_validator
from sqlmodel import Field, SQLModel


class AccountType(StrEnum):
    bank = "bank"
    brokerage = "brokerage"
    wallet = "wallet"
    card = "card"
    gift_card = "gift_card"


class TransferModule(StrEnum):
    spending = "spending"
    investing = "investing"


class CurrencyDisplayPreference(StrEnum):
    symbol = "symbol"
    code = "code"


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
        sa.UniqueConstraint("id", "workspace_id", name="uq_accounts_id_workspace"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_account_workspace_name"),
    )


class WorkspaceFinanceSetting(SQLModel, table=True):
    __tablename__ = "workspace_finance_settings"

    id: int | None = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspaces.id", unique=True, index=True)
    reporting_currency_code: str | None = Field(
        default=None, foreign_key="currencies.code", max_length=10
    )
    currency_display_preference: CurrencyDisplayPreference = Field(
        default=CurrencyDisplayPreference.symbol,
        sa_type=sa.String(length=24),
    )
    lookthrough_min_weight_pct: Decimal = Field(default=Decimal("0.5"), sa_type=sa.Numeric(7, 4))

    # Fallback account for spending-transaction creates that don't specify one
    # (spec-054) — every new transaction must resolve to an account somehow.
    # Nullable: workspaces need no default until they set one.
    default_spending_account_id: int | None = Field(default=None, index=True)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["default_spending_account_id", "workspace_id"],
            ["accounts.id", "accounts.workspace_id"],
            name="fk_workspace_finance_settings_default_spending_account",
        ),
    )


class UserFinanceSetting(SQLModel, table=True):
    __tablename__ = "user_finance_settings"

    id: int | None = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    reporting_currency_override_code: str | None = Field(
        default=None, foreign_key="currencies.code", max_length=10
    )
    currency_display_preference_override: CurrencyDisplayPreference | None = Field(
        default=None,
        sa_type=sa.String(length=24),
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            name="uq_user_finance_settings_workspace_user",
        ),
    )


class FxRate(SQLModel, table=True):
    """A system-fetched OR user-provided historical FX rate (spec-072).

    ``workspace_id`` NULL = system/global rate (all rows before spec-072,
    and all live-fetched rows going forward); set = a user-provided
    historical rate visible only to that workspace. Callers resolving a
    *live/current* rate must filter ``workspace_id IS NULL`` explicitly —
    user rows must never leak into present-day valuation (INV-3).
    """

    __tablename__ = "fx_rates"

    id: int | None = Field(default=None, primary_key=True)
    workspace_id: int | None = Field(
        default=None, foreign_key="workspaces.id", index=True, nullable=True
    )
    base_currency_code: str = Field(foreign_key="currencies.code", max_length=10, index=True)
    quote_currency_code: str = Field(foreign_key="currencies.code", max_length=10, index=True)
    rate: Decimal = Field(sa_type=sa.Numeric(precision=20, scale=10))
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
        sa.Index(
            "uq_fx_rate_user_row",
            "workspace_id",
            "base_currency_code",
            "quote_currency_code",
            "as_of",
            unique=True,
            postgresql_where=sa.text("workspace_id IS NOT NULL"),
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

    from_account_id: int = Field(index=True)
    to_account_id: int = Field(index=True)
    from_currency_code: str = Field(foreign_key="currencies.code", max_length=10, index=True)
    to_currency_code: str = Field(foreign_key="currencies.code", max_length=10, index=True)

    gross_amount: Decimal = Field(sa_type=sa.Numeric(precision=14, scale=2))
    fx_rate_used: Decimal | None = Field(default=None, sa_type=sa.Numeric(precision=20, scale=10))
    fx_fee_amount: Decimal = Field(default=Decimal("0"), sa_type=sa.Numeric(precision=14, scale=2))
    platform_fee_amount: Decimal = Field(
        default=Decimal("0"), sa_type=sa.Numeric(precision=14, scale=2)
    )
    tax_amount: Decimal = Field(default=Decimal("0"), sa_type=sa.Numeric(precision=14, scale=2))
    net_amount_received: Decimal = Field(sa_type=sa.Numeric(precision=14, scale=2))
    occurred_at: datetime = Field(sa_type=sa.DateTime(timezone=True), index=True)
    notes: str | None = Field(default=None, max_length=500)
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
            ["from_account_id", "workspace_id"],
            ["accounts.id", "accounts.workspace_id"],
            name="fk_capital_transfers_from_account_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["to_account_id", "workspace_id"],
            ["accounts.id", "accounts.workspace_id"],
            name="fk_capital_transfers_to_account_workspace",
        ),
    )


class NetWorthSnapshot(SQLModel, table=True):
    """A daily net-worth point: live-computed (default) or user-backfilled
    (spec-072). ``source='live'`` rows (the only kind the daily job writes)
    always populate all three components; a ``source='user_provided'`` row
    may carry only ``total_net_worth`` with the components left null (a
    bare-total point still draws the line but is excluded from the
    stacked-area component view). A live row for a date always wins — user
    points are only ever accepted for dates before the workspace's earliest
    live snapshot (INV-2, enforced at the service layer)."""

    __tablename__ = "net_worth_snapshots"

    id: int | None = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    snapshot_date: date = Field(sa_type=sa.Date())
    reporting_currency: str = Field(max_length=10)
    holdings_value: Decimal | None = Field(default=None, sa_type=sa.Numeric(precision=18, scale=2))
    investing_cash: Decimal | None = Field(default=None, sa_type=sa.Numeric(precision=18, scale=2))
    spending_cash: Decimal | None = Field(default=None, sa_type=sa.Numeric(precision=18, scale=2))
    total_net_worth: Decimal = Field(sa_type=sa.Numeric(precision=18, scale=2))
    fx_rates_used: dict = Field(default_factory=dict, sa_type=sa.JSON())
    source: str = Field(default="live", sa_type=sa.String(length=20))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id",
            "snapshot_date",
            name="uq_workspace_net_worth_snapshot_day",
        ),
        sa.CheckConstraint(
            "source IN ('live', 'user_provided')", name="ck_net_worth_snapshots_source"
        ),
        sa.CheckConstraint(
            "(source != 'live') OR "
            "(holdings_value IS NOT NULL AND investing_cash IS NOT NULL AND spending_cash IS NOT NULL)",
            name="ck_net_worth_snapshots_live_components_complete",
        ),
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
        return v
