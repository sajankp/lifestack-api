"""phase1 roadmap models recurring and performance

Revision ID: 0013_phase1_models
Revises: 0012_notif_weekly
Create Date: 2026-05-25 23:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013_phase1_models"
down_revision = "0012_notif_weekly"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recurring_transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("frequency", sa.String(length=16), nullable=False),
        sa.Column("interval", sa.Integer(), nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("next_due_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["category_id"], ["spending_categories.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index(
        op.f("ix_recurring_transactions_public_id"),
        "recurring_transactions",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_recurring_transactions_workspace_id"),
        "recurring_transactions",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_recurring_transactions_user_id"),
        "recurring_transactions",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_recurring_transactions_category_id"),
        "recurring_transactions",
        ["category_id"],
        unique=False,
    )

    op.add_column(
        "spending_transactions", sa.Column("recurring_transaction_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        op.f("ix_spending_transactions_recurring_transaction_id"),
        "spending_transactions",
        ["recurring_transaction_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_spending_transactions_recurring",
        "spending_transactions",
        "recurring_transactions",
        ["recurring_transaction_id"],
        ["id"],
    )

    op.create_table(
        "holding_prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("holding_id", sa.Integer(), nullable=False),
        sa.Column("price_date", sa.Date(), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["holding_id"], ["investing_holdings.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("holding_id", "price_date", name="uq_holding_price_day"),
    )
    op.create_index(
        op.f("ix_holding_prices_workspace_id"), "holding_prices", ["workspace_id"], unique=False
    )
    op.create_index(
        op.f("ix_holding_prices_holding_id"), "holding_prices", ["holding_id"], unique=False
    )

    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("total_value", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("total_cost", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("holdings_value", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("cash_value", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("currency_code", sa.String(length=10), nullable=False),
        sa.Column("fx_rates_used", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "snapshot_date", name="uq_snapshot_workspace_date"),
    )
    op.create_index(
        op.f("ix_portfolio_snapshots_workspace_id"),
        "portfolio_snapshots",
        ["workspace_id"],
        unique=False,
    )

    op.create_index(
        "ix_spending_transactions_workspace_occurred",
        "spending_transactions",
        ["workspace_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_spending_transactions_workspace_category_occurred",
        "spending_transactions",
        ["workspace_id", "category_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_spending_transactions_workspace_category_occurred", table_name="spending_transactions"
    )
    op.drop_index("ix_spending_transactions_workspace_occurred", table_name="spending_transactions")

    op.drop_index(op.f("ix_portfolio_snapshots_workspace_id"), table_name="portfolio_snapshots")
    op.drop_table("portfolio_snapshots")

    op.drop_index(op.f("ix_holding_prices_holding_id"), table_name="holding_prices")
    op.drop_index(op.f("ix_holding_prices_workspace_id"), table_name="holding_prices")
    op.drop_table("holding_prices")

    op.drop_constraint(
        "fk_spending_transactions_recurring", "spending_transactions", type_="foreignkey"
    )
    op.drop_index(
        op.f("ix_spending_transactions_recurring_transaction_id"),
        table_name="spending_transactions",
    )
    op.drop_column("spending_transactions", "recurring_transaction_id")

    op.drop_index(
        op.f("ix_recurring_transactions_category_id"), table_name="recurring_transactions"
    )
    op.drop_index(op.f("ix_recurring_transactions_user_id"), table_name="recurring_transactions")
    op.drop_index(
        op.f("ix_recurring_transactions_workspace_id"), table_name="recurring_transactions"
    )
    op.drop_index(op.f("ix_recurring_transactions_public_id"), table_name="recurring_transactions")
    op.drop_table("recurring_transactions")
