"""create notifications and weekly summaries

Revision ID: 0012_notif_weekly
Revises: 0011_create_exports
Create Date: 2026-05-25 22:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012_notif_weekly"
down_revision = "0011_create_exports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.String(length=2000), nullable=True),
        sa.Column("module", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column("entity_public_id", sa.Uuid(), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index(op.f("ix_notifications_public_id"), "notifications", ["public_id"], unique=True)
    op.create_index(
        op.f("ix_notifications_workspace_id"), "notifications", ["workspace_id"], unique=False
    )
    op.create_index(op.f("ix_notifications_user_id"), "notifications", ["user_id"], unique=False)

    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("channel_in_app", sa.Boolean(), nullable=False),
        sa.Column("channel_email", sa.Boolean(), nullable=False),
        sa.Column("channel_push", sa.Boolean(), nullable=False),
        sa.Column("is_muted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "user_id", "category", name="uq_notification_pref"),
    )
    op.create_index(
        op.f("ix_notification_preferences_workspace_id"),
        "notification_preferences",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_preferences_user_id"),
        "notification_preferences",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("notification_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_detail", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_notification_deliveries_notification_id"),
        "notification_deliveries",
        ["notification_id"],
        unique=False,
    )

    op.create_table(
        "weekly_summaries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("week_end", sa.Date(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("todo_summary", sa.JSON(), nullable=False),
        sa.Column("spending_summary", sa.JSON(), nullable=False),
        sa.Column("investing_summary", sa.JSON(), nullable=False),
        sa.Column("highlights", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint("workspace_id", "week_start", name="uq_weekly_summary_workspace_week"),
    )
    op.create_index(
        op.f("ix_weekly_summaries_public_id"), "weekly_summaries", ["public_id"], unique=True
    )
    op.create_index(
        op.f("ix_weekly_summaries_workspace_id"), "weekly_summaries", ["workspace_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_weekly_summaries_workspace_id"), table_name="weekly_summaries")
    op.drop_index(op.f("ix_weekly_summaries_public_id"), table_name="weekly_summaries")
    op.drop_table("weekly_summaries")

    op.drop_index(
        op.f("ix_notification_deliveries_notification_id"), table_name="notification_deliveries"
    )
    op.drop_table("notification_deliveries")

    op.drop_index(
        op.f("ix_notification_preferences_user_id"), table_name="notification_preferences"
    )
    op.drop_index(
        op.f("ix_notification_preferences_workspace_id"), table_name="notification_preferences"
    )
    op.drop_table("notification_preferences")

    op.drop_index(op.f("ix_notifications_user_id"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_workspace_id"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_public_id"), table_name="notifications")
    op.drop_table("notifications")
