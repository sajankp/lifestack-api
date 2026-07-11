"""add investing_dividends (spec-073)

A dividend/interest/coupon is a first-class income event: it credits an
investing_cash_balances row with no offsetting debit anywhere (unlike the
former workaround of a fake wallet->brokerage transfer). income_type is a
plain string + CHECK (not a native enum) to avoid the enum-migration churn
seen with named PG enums elsewhere in this codebase.

Revision ID: 0047_investing_dividends
Revises: 0046_health_module
Create Date: 2026-07-10 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0047_investing_dividends"
down_revision = "0046_health_module"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "investing_dividends",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("holding_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=20), nullable=True),
        sa.Column("income_type", sa.String(length=20), nullable=False, server_default="dividend"),
        sa.Column("gross_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column(
            "tax_withheld", sa.Numeric(precision=18, scale=2), nullable=False, server_default="0"
        ),
        sa.Column("net_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=10), nullable=False),
        sa.Column("pay_date", sa.Date(), nullable=False),
        sa.Column("external_ref", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id", "workspace_id"],
            ["accounts.id", "accounts.workspace_id"],
            name="fk_investing_dividends_account_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["holding_id"], ["investing_holdings.id"], name="fk_investing_dividends_holding"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_investing_dividends_user"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_investing_dividends_workspace"
        ),
        sa.ForeignKeyConstraint(
            ["currency"], ["currencies.code"], name="fk_investing_dividends_currency"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_investing_dividends_public_id"),
        sa.CheckConstraint(
            "income_type IN ('dividend', 'interest', 'coupon')",
            name="ck_investing_dividends_income_type",
        ),
        sa.CheckConstraint("gross_amount > 0", name="ck_investing_dividends_gross_positive"),
        sa.CheckConstraint("tax_withheld >= 0", name="ck_investing_dividends_tax_non_negative"),
        sa.CheckConstraint("net_amount > 0", name="ck_investing_dividends_net_positive"),
        sa.CheckConstraint(
            "net_amount = gross_amount - tax_withheld",
            name="ck_investing_dividends_net_equals_gross_minus_tax",
        ),
    )
    op.create_index("ix_investing_dividends_public_id", "investing_dividends", ["public_id"])
    op.create_index("ix_investing_dividends_workspace_id", "investing_dividends", ["workspace_id"])
    op.create_index("ix_investing_dividends_user_id", "investing_dividends", ["user_id"])
    op.create_index("ix_investing_dividends_account_id", "investing_dividends", ["account_id"])
    op.create_index("ix_investing_dividends_holding_id", "investing_dividends", ["holding_id"])
    op.create_index(
        "ix_investing_dividends_workspace_account_paydate",
        "investing_dividends",
        ["workspace_id", "account_id", "pay_date"],
    )
    op.create_index(
        "uq_investing_dividends_external_ref",
        "investing_dividends",
        ["workspace_id", "account_id", "external_ref"],
        unique=True,
        postgresql_where=sa.text("external_ref IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_investing_dividends_external_ref", "investing_dividends")
    op.drop_index("ix_investing_dividends_workspace_account_paydate", "investing_dividends")
    op.drop_index("ix_investing_dividends_holding_id", "investing_dividends")
    op.drop_index("ix_investing_dividends_account_id", "investing_dividends")
    op.drop_index("ix_investing_dividends_user_id", "investing_dividends")
    op.drop_index("ix_investing_dividends_workspace_id", "investing_dividends")
    op.drop_index("ix_investing_dividends_public_id", "investing_dividends")
    op.drop_table("investing_dividends")
