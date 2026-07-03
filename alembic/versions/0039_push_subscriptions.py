"""add push_subscriptions and todos.reminded_at (spec-052: web push delivery)

Push-subscription store for Web Push delivery, plus the reminded_at dedup
column on todos that todo_reminder_job uses to avoid re-notifying the same
due todo on every run.

Revision ID: 0039_push_subscriptions
Revises: 0038_import_batch_extra_json
Create Date: 2026-07-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0039_push_subscriptions"
down_revision = "0038_import_batch_extra_json"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.String(length=1000), nullable=False),
        sa.Column("p256dh", sa.String(length=255), nullable=False),
        sa.Column("auth", sa.String(length=255), nullable=False),
        sa.Column("device_label", sa.String(length=100), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_push_subscriptions_workspace"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_push_subscriptions_user"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_push_subscriptions_public_id"),
        sa.UniqueConstraint("endpoint", name="uq_push_subscriptions_endpoint"),
    )
    op.create_index("ix_push_subscriptions_public_id", "push_subscriptions", ["public_id"])
    op.create_index("ix_push_subscriptions_workspace_id", "push_subscriptions", ["workspace_id"])
    op.create_index("ix_push_subscriptions_user_id", "push_subscriptions", ["user_id"])

    op.add_column("todos", sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("todos", "reminded_at")

    op.drop_index("ix_push_subscriptions_user_id", "push_subscriptions")
    op.drop_index("ix_push_subscriptions_workspace_id", "push_subscriptions")
    op.drop_index("ix_push_subscriptions_public_id", "push_subscriptions")
    op.drop_table("push_subscriptions")
