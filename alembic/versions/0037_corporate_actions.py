"""add investing_corporate_actions (splits, reverse splits, bonus issues)

Spec-051: a corporate action is replayed inside InvestingOrderService's FIFO
replay alongside orders. A split/reverse-split scales existing OrderLot rows
in place; a bonus issue creates a new zero-cost lot with no originating buy
order, so OrderLot.buy_order_id becomes nullable and gains a sibling
corporate_action_id FK (exactly one of the two set, enforced by CHECK).

No data backfill: existing OrderLot rows all have buy_order_id set and
corporate_action_id null, satisfying the new CHECK constraint unchanged.

Revision ID: 0037_corporate_actions
Revises: 0036_cost_basis_fees_precision
Create Date: 2026-07-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0037_corporate_actions"
down_revision = "0036_cost_basis_fees_precision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # op.create_table issues CREATE TYPE automatically for named enum columns
    # — no explicit sa.Enum(...).create() pre-create (see migration 0033).
    op.create_table(
        "investing_corporate_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column(
            "action_type",
            sa.Enum("split", "bonus", name="investing_corporate_action_type"),
            nullable=False,
        ),
        sa.Column("ratio_base", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("ratio_quote", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("notes", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id", "workspace_id"],
            ["accounts.id", "accounts.workspace_id"],
            name="fk_investing_corporate_actions_account_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_investing_corporate_actions_user"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_investing_corporate_actions_workspace"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_investing_corporate_actions_public_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "account_id",
            "symbol",
            "ex_date",
            "action_type",
            name="uq_corporate_action_workspace_account_symbol_exdate_type",
        ),
    )
    op.create_index(
        "ix_investing_corporate_actions_public_id", "investing_corporate_actions", ["public_id"]
    )
    op.create_index(
        "ix_investing_corporate_actions_workspace_id",
        "investing_corporate_actions",
        ["workspace_id"],
    )
    op.create_index(
        "ix_investing_corporate_actions_user_id", "investing_corporate_actions", ["user_id"]
    )
    op.create_index(
        "ix_investing_corporate_actions_account_id", "investing_corporate_actions", ["account_id"]
    )
    op.create_index(
        "ix_investing_corporate_actions_workspace_symbol_account",
        "investing_corporate_actions",
        ["workspace_id", "symbol", "account_id"],
    )

    # OrderLot.buy_order_id becomes optional; a bonus-issue lot has no
    # originating buy order and is linked via corporate_action_id instead.
    op.alter_column(
        "investing_order_lots",
        "buy_order_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.add_column(
        "investing_order_lots",
        sa.Column("corporate_action_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_investing_order_lots_corporate_action",
        "investing_order_lots",
        "investing_corporate_actions",
        ["corporate_action_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_investing_order_lots_corporate_action_id",
        "investing_order_lots",
        ["corporate_action_id"],
    )
    op.create_check_constraint(
        "ck_investing_order_lots_exactly_one_origin",
        "investing_order_lots",
        "(buy_order_id IS NOT NULL AND corporate_action_id IS NULL) OR "
        "(buy_order_id IS NULL AND corporate_action_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_investing_order_lots_exactly_one_origin", "investing_order_lots", type_="check"
    )
    op.drop_index("ix_investing_order_lots_corporate_action_id", "investing_order_lots")
    op.drop_constraint(
        "fk_investing_order_lots_corporate_action", "investing_order_lots", type_="foreignkey"
    )
    op.drop_column("investing_order_lots", "corporate_action_id")
    op.alter_column(
        "investing_order_lots",
        "buy_order_id",
        existing_type=sa.Integer(),
        nullable=False,
    )

    op.drop_index(
        "ix_investing_corporate_actions_workspace_symbol_account", "investing_corporate_actions"
    )
    op.drop_index("ix_investing_corporate_actions_account_id", "investing_corporate_actions")
    op.drop_index("ix_investing_corporate_actions_user_id", "investing_corporate_actions")
    op.drop_index("ix_investing_corporate_actions_workspace_id", "investing_corporate_actions")
    op.drop_index("ix_investing_corporate_actions_public_id", "investing_corporate_actions")
    op.drop_table("investing_corporate_actions")

    investing_corporate_action_type = sa.Enum(
        "split", "bonus", name="investing_corporate_action_type"
    )
    investing_corporate_action_type.drop(op.get_bind(), checkfirst=True)
