"""Add OAuth fields to users table.

Revision ID: 0061_add_oauth_fields
Revises: 0060_user_timezone
Create Date: 2025-08-04 22:45:00.000000
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0061_add_oauth_fields"
down_revision = "0060_user_timezone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("oauth_provider", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("oauth_sub", sa.String(length=128), nullable=True),
    )
    op.create_index("ix_users_oauth_provider", "users", ["oauth_provider"])
    op.create_index("ix_users_oauth_sub", "users", ["oauth_sub"])
    op.create_unique_constraint(
        "uq_users_oauth_provider_sub",
        "users",
        ["oauth_provider", "oauth_sub"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_users_oauth_provider_sub", "users", type_="unique")
    op.drop_index("ix_users_oauth_sub", table_name="users")
    op.drop_index("ix_users_oauth_provider", table_name="users")
    op.drop_column("users", "oauth_sub")
    op.drop_column("users", "oauth_provider")
