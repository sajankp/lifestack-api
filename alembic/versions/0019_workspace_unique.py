"""add_workspace_membership_uniqueness

Revision ID: 0019_workspace_unique
Revises: 0018_user_finance_settings
Create Date: 2026-05-31 22:12:03.546704
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0019_workspace_unique"
down_revision = "0018_user_finance_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_workspace_membership_workspace_user",
        "workspace_memberships",
        ["workspace_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_workspace_membership_workspace_user", "workspace_memberships", type_="unique"
    )
