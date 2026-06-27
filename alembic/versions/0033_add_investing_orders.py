"""add investing orders and cash balance trigger tracking

Revision ID: 0033_add_investing_orders
Revises: 0032_hybrid_instrument_catalog
Create Date: 2026-06-27 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0033_add_investing_orders"
down_revision = "0032_hybrid_instrument_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add trigger_type and trigger_ref to investing_cash_balances
    op.add_column(
        "investing_cash_balances",
        sa.Column("trigger_type", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "investing_cash_balances",
        sa.Column("trigger_ref", sa.Uuid(), nullable=True),
    )

    # Create investing_order_type enum
    investing_order_type = sa.Enum("buy", "sell", name="investing_order_type")
    investing_order_type.create(op.get_bind(), checkfirst=True)

    # Create investing_orders table
    op.create_table(
        "investing_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column(
            "order_type",
            sa.Enum("buy", "sell", name="investing_order_type"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("price_per_unit", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("gross_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column(
            "brokerage_fee", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
        ),
        sa.Column(
            "tax_amount", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
        ),
        sa.Column(
            "other_fees", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"
        ),
        sa.Column("net_amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=10), nullable=False),
        sa.Column("exchange_name", sa.String(length=50), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("realized_gain_loss", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("avg_cost_at_sale", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("source_import_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id", "workspace_id"],
            ["accounts.id", "accounts.workspace_id"],
            name="fk_investing_orders_account_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["investing_instruments.id"],
            name="fk_investing_orders_instrument",
        ),
        sa.ForeignKeyConstraint(
            ["source_import_id"],
            ["import_batches.id"],
            name="fk_investing_orders_import_batch",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_investing_orders_user",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_investing_orders_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_investing_orders_public_id"),
    )
    op.create_index("ix_investing_orders_public_id", "investing_orders", ["public_id"])
    op.create_index("ix_investing_orders_workspace_id", "investing_orders", ["workspace_id"])
    op.create_index("ix_investing_orders_user_id", "investing_orders", ["user_id"])
    op.create_index("ix_investing_orders_account_id", "investing_orders", ["account_id"])
    op.create_index("ix_investing_orders_instrument_id", "investing_orders", ["instrument_id"])
    op.create_index("ix_investing_orders_source_type", "investing_orders", ["source_type"])
    op.create_index(
        "ix_investing_orders_source_import_id", "investing_orders", ["source_import_id"]
    )
    op.create_index(
        "ix_investing_orders_workspace_symbol_account",
        "investing_orders",
        ["workspace_id", "symbol", "account_id"],
    )
    op.create_index(
        "ix_investing_orders_workspace_occurred_at",
        "investing_orders",
        ["workspace_id", "occurred_at"],
    )
    op.create_index(
        "ix_investing_orders_workspace_import",
        "investing_orders",
        ["workspace_id", "source_import_id"],
    )


def downgrade() -> None:
    op.drop_table("investing_orders")

    investing_order_type = sa.Enum("buy", "sell", name="investing_order_type")
    investing_order_type.drop(op.get_bind(), checkfirst=True)

    op.drop_column("investing_cash_balances", "trigger_ref")
    op.drop_column("investing_cash_balances", "trigger_type")
