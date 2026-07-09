"""add todos.parent_id (one-level subtasks, spec-068)

Revision ID: 0045_todo_parent_id
Revises: 0044_create_net_worth_snapshots
Create Date: 2026-07-09 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0045_todo_parent_id"
down_revision = "0044_create_net_worth_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("todos", sa.Column("parent_id", sa.Integer(), nullable=True))
    op.create_index("ix_todos_parent_id", "todos", ["parent_id"])
    op.create_foreign_key(
        "fk_todos_parent_id_todos",
        "todos",
        "todos",
        ["parent_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_todos_parent_id_todos", "todos", type_="foreignkey")
    op.drop_index("ix_todos_parent_id", table_name="todos")
    op.drop_column("todos", "parent_id")
