"""add locale/decimal-place display profile fields (spec-075)

Revision ID: 0049_currency_display_profile
Revises: 0048_historical_data_ingestion
"""

import sqlalchemy as sa

from alembic import op

revision = "0049_currency_display_profile"
down_revision = "0048_historical_data_ingestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_finance_settings",
        sa.Column("locale", sa.String(length=16), nullable=False, server_default="en-US"),
    )
    op.add_column(
        "workspace_finance_settings",
        sa.Column("decimal_places", sa.Integer(), nullable=False, server_default="2"),
    )
    op.add_column(
        "user_finance_settings",
        sa.Column("locale_override", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "user_finance_settings",
        sa.Column("decimal_places_override", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_finance_settings", "decimal_places_override")
    op.drop_column("user_finance_settings", "locale_override")
    op.drop_column("workspace_finance_settings", "decimal_places")
    op.drop_column("workspace_finance_settings", "locale")
