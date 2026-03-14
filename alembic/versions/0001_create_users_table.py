"""create users table

Revision ID: 0001_create_users_table
Revises:
Create Date: 2025-01-01 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001_create_users_table"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=50), nullable=False),
        sa.Column("hashed_password", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint("username"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=False)
    op.create_index(op.f("ix_users_public_id"), "users", ["public_id"], unique=False)
    op.create_index(op.f("ix_users_username"), "users", ["username"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_users_username"), table_name="users")
    op.drop_index(op.f("ix_users_public_id"), table_name="users")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
