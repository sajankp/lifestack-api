"""add remaining composite indexes for high-frequency query patterns (perf optimization, cont'd)

Revision ID: 0057_composite_indexes_perf_2
Revises: 0056_composite_indexes_perf
Create Date: 2026-07-19 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0057_composite_indexes_perf_2"
down_revision = "0056_composite_indexes_perf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # spending_transactions(workspace_id, category_id, occurred_at)
    # Optimizes: category breakdown queries
    op.create_index(
        "ix_spending_transactions_workspace_category_occurred_at",
        "spending_transactions",
        ["workspace_id", "category_id", "occurred_at"],
    )

    # investing_orders(workspace_id, user_id, occurred_at DESC)
    # Optimizes: order history listing by workspace/user ordered by date
    op.create_index(
        "ix_investing_orders_workspace_user_occurred_at_desc",
        "investing_orders",
        ["workspace_id", "user_id", sa.text("occurred_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_investing_orders_workspace_user_occurred_at_desc", table_name="investing_orders"
    )
    op.drop_index(
        "ix_spending_transactions_workspace_category_occurred_at",
        table_name="spending_transactions",
    )
