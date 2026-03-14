"""create workspaces and memberships

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-14 11:45:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_create_workspaces"
down_revision: str | None = "0002_create_todos_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Create workspaces table
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index(op.f("ix_workspaces_public_id"), "workspaces", ["public_id"], unique=True)

    # 2. Create workspace_memberships table
    op.create_table(
        "workspace_memberships",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_workspace_memberships_user_id"), "workspace_memberships", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_workspace_memberships_workspace_id"),
        "workspace_memberships",
        ["workspace_id"],
        unique=False,
    )

    # 3. Transition todos table
    # Since we are in development, we can drop and recreate the column for simplicity.
    # In production, we would add nullable=True, migrate data, then set nullable=False.
    op.drop_column("todos", "workspace_id")
    op.add_column("todos", sa.Column("workspace_id", sa.Integer(), nullable=False))
    op.create_foreign_key("fk_todos_workspace_id", "todos", "workspaces", ["workspace_id"], ["id"])
    op.create_index(op.f("ix_todos_workspace_id"), "todos", ["workspace_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_todos_workspace_id"), table_name="todos")
    op.drop_constraint("fk_todos_workspace_id", "todos", type_="foreignkey")
    op.drop_column("todos", "workspace_id")
    op.add_column("todos", sa.Column("workspace_id", sa.Uuid(), nullable=False))

    op.drop_index(op.f("ix_workspace_memberships_workspace_id"), table_name="workspace_memberships")
    op.drop_index(op.f("ix_workspace_memberships_user_id"), table_name="workspace_memberships")
    op.drop_table("workspace_memberships")

    op.drop_index(op.f("ix_workspaces_public_id"), table_name="workspaces")
    op.drop_table("workspaces")
