"""create recurring todo rules

Revision ID: 0014_recurring_todo_rules
Revises: 0013_phase1_models
Create Date: 2026-05-27 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_recurring_todo_rules"
down_revision: str | None = "0013_phase1_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recurring_todo_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("priority", sa.String(), nullable=False),
        sa.Column("frequency", sa.String(length=16), nullable=False),
        sa.Column("interval", sa.Integer(), nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("next_due_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_recurring_todo_rules_public_id"),
        "recurring_todo_rules",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_recurring_todo_rules_workspace_id"),
        "recurring_todo_rules",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_recurring_todo_rules_user_id"), "recurring_todo_rules", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_recurring_todo_rules_user_id"), table_name="recurring_todo_rules")
    op.drop_index(op.f("ix_recurring_todo_rules_workspace_id"), table_name="recurring_todo_rules")
    op.drop_index(op.f("ix_recurring_todo_rules_public_id"), table_name="recurring_todo_rules")
    op.drop_table("recurring_todo_rules")
