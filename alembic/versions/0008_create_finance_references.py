"""create finance reference tables

Revision ID: 0008_create_finance_references
Revises: 0007_create_investing
Create Date: 2026-05-23 23:50:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
import sqlmodel

from alembic import op

# revision identifiers, used by Alembic.
revision = "0008_create_finance_references"
down_revision = "0007_create_investing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "currencies",
        sa.Column("code", sqlmodel.sql.sqltypes.AutoString(length=10), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("symbol", sqlmodel.sql.sqltypes.AutoString(length=8), nullable=True),
        sa.Column("minor_unit", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("code"),
    )

    op.create_table(
        "workspace_currencies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("currency_code", sqlmodel.sql.sqltypes.AutoString(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["currency_code"], ["currencies.code"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "currency_code", name="uq_workspace_currency"),
    )
    op.create_index(
        op.f("ix_workspace_currencies_workspace_id"),
        "workspace_currencies",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workspace_currencies_currency_code"),
        "workspace_currencies",
        ["currency_code"],
        unique=False,
    )

    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
        sa.Column("account_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "default_currency_code", sqlmodel.sql.sqltypes.AutoString(length=10), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["default_currency_code"], ["currencies.code"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_account_workspace_name"),
    )
    op.create_index(op.f("ix_accounts_public_id"), "accounts", ["public_id"], unique=True)
    op.create_index(op.f("ix_accounts_workspace_id"), "accounts", ["workspace_id"], unique=False)
    op.create_index(
        op.f("ix_accounts_default_currency_code"),
        "accounts",
        ["default_currency_code"],
        unique=False,
    )

    op.create_table(
        "workspace_finance_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column(
            "reporting_currency_code", sqlmodel.sql.sqltypes.AutoString(length=10), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["reporting_currency_code"], ["currencies.code"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_workspace_finance_settings_workspace_id"),
        "workspace_finance_settings",
        ["workspace_id"],
        unique=True,
    )

    # Seed initial currencies.
    op.execute(
        """
        INSERT INTO currencies (code, name, symbol, minor_unit, is_active, created_at, updated_at)
        VALUES
          ('INR', 'Indian Rupee', '₹', 2, true, now(), now()),
          ('USD', 'US Dollar', '$', 2, true, now(), now()),
          ('GBP', 'British Pound', '£', 2, true, now(), now())
        """
    )

    # Backfill workspace currency allow-list from existing spending/investing rows.
    op.execute(
        """
        INSERT INTO workspace_currencies (workspace_id, currency_code, created_at)
        SELECT DISTINCT workspace_id, upper(currency), now()
        FROM investing_holdings
        WHERE currency IS NOT NULL
          AND upper(currency) IN ('INR', 'USD', 'GBP')
        ON CONFLICT (workspace_id, currency_code) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO workspace_currencies (workspace_id, currency_code, created_at)
        SELECT DISTINCT workspace_id, upper(currency), now()
        FROM investing_cash_balances
        WHERE currency IS NOT NULL
          AND upper(currency) IN ('INR', 'USD', 'GBP')
        ON CONFLICT (workspace_id, currency_code) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_workspace_finance_settings_workspace_id"),
        table_name="workspace_finance_settings",
    )
    op.drop_table("workspace_finance_settings")

    op.drop_index(op.f("ix_accounts_default_currency_code"), table_name="accounts")
    op.drop_index(op.f("ix_accounts_workspace_id"), table_name="accounts")
    op.drop_index(op.f("ix_accounts_public_id"), table_name="accounts")
    op.drop_table("accounts")

    op.drop_index(op.f("ix_workspace_currencies_currency_code"), table_name="workspace_currencies")
    op.drop_index(op.f("ix_workspace_currencies_workspace_id"), table_name="workspace_currencies")
    op.drop_table("workspace_currencies")

    op.drop_table("currencies")
