"""Spec-078: wallet ledger reconciliation (statement matching).

Two new tables only. Matching is metadata, never mutation (INV-1): these
tables never cause a write to `spending_transactions` or `capital_transfers`,
and `investing_cash_balances` is untouched (INV-2 — wallet accounts get no
snapshot semantics).
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class StatementLineMatchLeg(StrEnum):
    from_leg = "from"
    to_leg = "to"


class AccountStatement(SQLModel, table=True):
    __tablename__ = "account_statements"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, unique=True, index=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    account_id: int = Field(foreign_key="accounts.id", index=True)

    period_start: date
    period_end: date
    # Reference value only (INV-2) — never written into investing_cash_balances.
    closing_balance: Decimal | None = Field(default=None, sa_type=sa.Numeric(precision=14, scale=2))
    currency_code: str = Field(foreign_key="currencies.code", max_length=10)

    import_batch_id: int | None = Field(default=None, foreign_key="import_batches.id", index=True)

    # Informational only (owner decision, spec-078): not a lock, not load-bearing
    # for any other computation. Cleared whenever a breaking edit invalidates a
    # match inside this statement's period.
    reconciled_through: date | None = Field(default=None)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint("id", "workspace_id", name="uq_account_statements_id_workspace"),
    )


class StatementLine(SQLModel, table=True):
    __tablename__ = "statement_lines"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, unique=True, index=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    account_id: int = Field(foreign_key="accounts.id", index=True)
    statement_id: int = Field(foreign_key="account_statements.id", index=True)

    occurred_at: date = Field(index=True)
    description: str = Field(max_length=500)
    # Signed: positive = credit (money in), negative = debit (money out).
    amount: Decimal = Field(sa_type=sa.Numeric(precision=14, scale=2))
    balance: Decimal | None = Field(default=None, sa_type=sa.Numeric(precision=14, scale=2))

    # Deterministic identity derived from (account, date, amount, description,
    # within-file duplicate index) — INV-4, re-uploading an overlapping
    # statement must not duplicate lines.
    external_ref: str = Field(max_length=64, index=True)

    matched_transaction_id: int | None = Field(
        default=None, foreign_key="spending_transactions.id", index=True
    )
    matched_transfer_id: int | None = Field(
        default=None, foreign_key="capital_transfers.id", index=True
    )
    matched_transfer_leg: StatementLineMatchLeg | None = Field(
        default=None, sa_type=sa.String(length=10)
    )
    matched_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "account_id", "external_ref", name="uq_statement_lines_account_external_ref"
        ),
        sa.CheckConstraint(
            "NOT (matched_transaction_id IS NOT NULL AND matched_transfer_id IS NOT NULL)",
            name="ck_statement_lines_exactly_one_match_target",
        ),
        sa.CheckConstraint(
            "(matched_transfer_leg IS NULL) OR (matched_transfer_id IS NOT NULL)",
            name="ck_statement_lines_leg_requires_transfer",
        ),
    )
