import uuid
from datetime import UTC, datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class AccountType(StrEnum):
    bank = "bank"
    brokerage = "brokerage"
    wallet = "wallet"


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
