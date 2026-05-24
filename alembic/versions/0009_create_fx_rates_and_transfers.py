"""create fx rates and capital transfers

Revision ID: 0009_fx_rates_transfers
Revises: 0008_create_finance_references
Create Date: 2026-05-24 10:45:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
import sqlmodel

from alembic import op

# revision identifiers, used by Alembic.
revision = "0009_fx_rates_transfers"
down_revision = "0008_create_finance_references"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fx_rates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "base_currency_code", sqlmodel.sql.sqltypes.AutoString(length=10), nullable=False
        ),
        sa.Column(
            "quote_currency_code", sqlmodel.sql.sqltypes.AutoString(length=10), nullable=False
        ),
        sa.Column("rate", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["base_currency_code"], ["currencies.code"]),
        sa.ForeignKeyConstraint(["quote_currency_code"], ["currencies.code"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "base_currency_code",
            "quote_currency_code",
            "as_of",
            "source",
            name="uq_fx_rate_pair_asof_source",
        ),
    )
    op.create_index(op.f("ix_fx_rates_as_of"), "fx_rates", ["as_of"], unique=False)
    op.create_index(
        op.f("ix_fx_rates_base_currency_code"), "fx_rates", ["base_currency_code"], unique=False
    )
    op.create_index(op.f("ix_fx_rates_fetched_at"), "fx_rates", ["fetched_at"], unique=False)
    op.create_index(
        op.f("ix_fx_rates_quote_currency_code"), "fx_rates", ["quote_currency_code"], unique=False
    )
    op.create_index(op.f("ix_fx_rates_source"), "fx_rates", ["source"], unique=False)

    op.create_table(
        "capital_transfers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("from_module", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("to_module", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("from_account_id", sa.Integer(), nullable=False),
        sa.Column("to_account_id", sa.Integer(), nullable=False),
        sa.Column(
            "from_currency_code", sqlmodel.sql.sqltypes.AutoString(length=10), nullable=False
        ),
        sa.Column("to_currency_code", sqlmodel.sql.sqltypes.AutoString(length=10), nullable=False),
        sa.Column("gross_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("fx_rate_used", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("fx_fee_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("platform_fee_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("tax_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("net_amount_received", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sqlmodel.sql.sqltypes.AutoString(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["from_account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["to_account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["from_currency_code"], ["currencies.code"]),
        sa.ForeignKeyConstraint(["to_currency_code"], ["currencies.code"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_capital_transfers_public_id"), "capital_transfers", ["public_id"], unique=True
    )
    op.create_index(
        op.f("ix_capital_transfers_workspace_id"),
        "capital_transfers",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_capital_transfers_actor_id"), "capital_transfers", ["actor_id"], unique=False
    )
    op.create_index(
        op.f("ix_capital_transfers_occurred_at"),
        "capital_transfers",
        ["occurred_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_capital_transfers_from_account_id"),
        "capital_transfers",
        ["from_account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_capital_transfers_to_account_id"),
        "capital_transfers",
        ["to_account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_capital_transfers_from_currency_code"),
        "capital_transfers",
        ["from_currency_code"],
        unique=False,
    )
    op.create_index(
        op.f("ix_capital_transfers_to_currency_code"),
        "capital_transfers",
        ["to_currency_code"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_capital_transfers_to_currency_code"), table_name="capital_transfers")
    op.drop_index(op.f("ix_capital_transfers_from_currency_code"), table_name="capital_transfers")
    op.drop_index(op.f("ix_capital_transfers_to_account_id"), table_name="capital_transfers")
    op.drop_index(op.f("ix_capital_transfers_from_account_id"), table_name="capital_transfers")
    op.drop_index(op.f("ix_capital_transfers_occurred_at"), table_name="capital_transfers")
    op.drop_index(op.f("ix_capital_transfers_actor_id"), table_name="capital_transfers")
    op.drop_index(op.f("ix_capital_transfers_workspace_id"), table_name="capital_transfers")
    op.drop_index(op.f("ix_capital_transfers_public_id"), table_name="capital_transfers")
    op.drop_table("capital_transfers")

    op.drop_index(op.f("ix_fx_rates_source"), table_name="fx_rates")
    op.drop_index(op.f("ix_fx_rates_quote_currency_code"), table_name="fx_rates")
    op.drop_index(op.f("ix_fx_rates_fetched_at"), table_name="fx_rates")
    op.drop_index(op.f("ix_fx_rates_base_currency_code"), table_name="fx_rates")
    op.drop_index(op.f("ix_fx_rates_as_of"), table_name="fx_rates")
    op.drop_table("fx_rates")
