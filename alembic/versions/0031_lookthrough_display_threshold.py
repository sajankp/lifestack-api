"""add workspace look-through display threshold

Revision ID: 0031_lookthrough_threshold
Revises: 0030_holding_price_cascade
"""

import sqlalchemy as sa

from alembic import op

revision = "0031_lookthrough_threshold"
down_revision = "0030_holding_price_cascade"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_finance_settings",
        sa.Column(
            "lookthrough_min_weight_pct",
            sa.Numeric(7, 4),
            nullable=False,
            server_default="0.5",
        ),
    )


def downgrade() -> None:
    op.drop_column("workspace_finance_settings", "lookthrough_min_weight_pct")
