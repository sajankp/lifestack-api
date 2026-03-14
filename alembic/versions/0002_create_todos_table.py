"""create todos table

Revision ID: 0002_create_todos_table
Revises: 0001_create_users_table
Create Date: 2026-03-14 11:15:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_create_todos_table"
down_revision = "0001_create_users_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "todos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("priority", sa.String(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index(op.f("ix_todos_public_id"), "todos", ["public_id"], unique=True)
    op.create_index(op.f("ix_todos_user_id"), "todos", ["user_id"], unique=False)
    op.create_index(op.f("ix_todos_workspace_id"), "todos", ["workspace_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_todos_workspace_id"), table_name="todos")
    op.drop_index(op.f("ix_todos_user_id"), table_name="todos")
    op.drop_index(op.f("ix_todos_public_id"), table_name="todos")
    op.drop_table("todos")
