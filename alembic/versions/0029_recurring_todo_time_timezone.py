"""add recurring todo time and timezone

Revision ID: 0029_recurring_todo_time
Revises: 5a08b11007af
"""

import sqlalchemy as sa

from alembic import op

revision = "0029_recurring_todo_time"
down_revision = "5a08b11007af"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("recurring_todo_rules", sa.Column("due_time", sa.Time(), nullable=True))
    op.add_column(
        "recurring_todo_rules",
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
    )
    op.alter_column("recurring_todo_rules", "timezone", server_default=None)


def downgrade() -> None:
    op.drop_column("recurring_todo_rules", "timezone")
    op.drop_column("recurring_todo_rules", "due_time")
