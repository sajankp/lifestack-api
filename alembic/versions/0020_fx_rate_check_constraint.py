"""add_fx_rate_same_currency_check_constraint

Revision ID: 0020_fx_rate_check_constraint
Revises: 0019_workspace_unique
Create Date: 2026-06-01 06:55:00.000000
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0020_fx_rate_check_constraint"
down_revision = "0019_workspace_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_fx_rates_same_currency_rate",
        "fx_rates",
        "UPPER(base_currency_code) <> UPPER(quote_currency_code) OR rate = 1.0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_fx_rates_same_currency_rate", "fx_rates", type_="check")
