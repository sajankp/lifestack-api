"""create investing module tables

Revision ID: 0007_create_investing
Revises: 1338de0c87a2
Create Date: 2026-05-23 20:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
import sqlmodel

from alembic import op

# revision identifiers, used by Alembic.
revision = "0007_create_investing"
down_revision = "1338de0c87a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "investing_holdings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
        sa.Column("account_name", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("avg_cost", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sqlmodel.sql.sqltypes.AutoString(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "symbol", "account_name", name="uq_holding_workspace_symbol_account"
        ),
    )
    op.create_index(
        op.f("ix_investing_holdings_public_id"), "investing_holdings", ["public_id"], unique=True
    )
    op.create_index(
        op.f("ix_investing_holdings_workspace_id"),
        "investing_holdings",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_investing_holdings_user_id"), "investing_holdings", ["user_id"], unique=False
    )

    op.create_table(
        "investing_cash_balances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_name", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
        sa.Column("balance", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sqlmodel.sql.sqltypes.AutoString(length=10), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_investing_cash_balances_public_id"),
        "investing_cash_balances",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_investing_cash_balances_workspace_id"),
        "investing_cash_balances",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_investing_cash_balances_user_id"),
        "investing_cash_balances",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_investing_cash_balances_user_id"), table_name="investing_cash_balances")
    op.drop_index(
        op.f("ix_investing_cash_balances_workspace_id"), table_name="investing_cash_balances"
    )
    op.drop_index(
        op.f("ix_investing_cash_balances_public_id"), table_name="investing_cash_balances"
    )
    op.drop_table("investing_cash_balances")

    op.drop_index(op.f("ix_investing_holdings_user_id"), table_name="investing_holdings")
    op.drop_index(op.f("ix_investing_holdings_workspace_id"), table_name="investing_holdings")
    op.drop_index(op.f("ix_investing_holdings_public_id"), table_name="investing_holdings")
    op.drop_table("investing_holdings")
