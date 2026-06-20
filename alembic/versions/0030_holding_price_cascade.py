"""cascade holding prices when a holding is deleted

Revision ID: 0030_holding_price_cascade
Revises: 0029_recurring_todo_time
Create Date: 2026-06-20 23:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "0030_holding_price_cascade"
down_revision = "0029_recurring_todo_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "holding_prices_holding_id_fkey",
        "holding_prices",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_holding_prices_holding",
        "holding_prices",
        "investing_holdings",
        ["holding_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_holding_prices_holding",
        "holding_prices",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "holding_prices_holding_id_fkey",
        "holding_prices",
        "investing_holdings",
        ["holding_id"],
        ["id"],
    )
