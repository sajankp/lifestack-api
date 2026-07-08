"""create net_worth_snapshots table

Revision ID: 0044_create_net_worth_snapshots
Revises: 0043_category_groups_budgets
Create Date: 2026-07-08 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0044_create_net_worth_snapshots"
down_revision = "0043_category_groups_budgets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "net_worth_snapshots",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("reporting_currency", sa.String(length=10), nullable=False),
        sa.Column("holdings_value", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("investing_cash", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("spending_cash", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("total_net_worth", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("fx_rates_used", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_net_worth_snapshots_workspace",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "snapshot_date",
            name="uq_workspace_net_worth_snapshot_day",
        ),
    )
    op.create_index(
        "ix_net_worth_snapshots_workspace_id",
        "net_worth_snapshots",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_net_worth_snapshots_workspace_id", table_name="net_worth_snapshots")
    op.drop_table("net_worth_snapshots")
