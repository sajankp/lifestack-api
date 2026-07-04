"""add workspace_finance_settings.default_spending_account_id (spec-054)

Fallback account for spending-transaction creates that don't specify one.
Nullable, no data migration — workspaces need no default until they set
one, and enforcement (account required at create time, via an explicit
account_id or this default) is forward-only.

Revision ID: 0041_default_spending_account
Revises: 0040_calendar_recurrence_modes
Create Date: 2026-07-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0041_default_spending_account"
down_revision = "0040_calendar_recurrence_modes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_finance_settings",
        sa.Column("default_spending_account_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_workspace_finance_settings_default_spending_account_id",
        "workspace_finance_settings",
        ["default_spending_account_id"],
    )
    op.create_foreign_key(
        "fk_workspace_finance_settings_default_spending_account",
        "workspace_finance_settings",
        "accounts",
        ["default_spending_account_id", "workspace_id"],
        ["id", "workspace_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_workspace_finance_settings_default_spending_account",
        "workspace_finance_settings",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_workspace_finance_settings_default_spending_account_id",
        "workspace_finance_settings",
    )
    op.drop_column("workspace_finance_settings", "default_spending_account_id")
