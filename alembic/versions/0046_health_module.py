"""health module: medications, medication_events, weight_entries (spec-069)

Revision ID: 0046_health_module
Revises: 0045_todo_parent_id
Create Date: 2026-07-09 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0046_health_module"
down_revision = "0045_todo_parent_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "medications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("dose_text", sa.String(length=60), nullable=True),
        sa.Column("refill_note", sa.String(length=500), nullable=True),
        sa.Column("frequency", sa.String(length=16), nullable=False),
        sa.Column("interval", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("days_of_week", sa.JSON(), nullable=True),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("times", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "reminders_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("last_reminded_slot", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("source_import_id", sa.Integer(), nullable=True),
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
        sa.CheckConstraint(
            "frequency IN ('daily', 'weekly', 'monthly')", name="ck_medications_frequency"
        ),
        sa.CheckConstraint("interval >= 1", name="ck_medications_interval_positive"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_medications_workspace"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_medications_user"),
        sa.ForeignKeyConstraint(
            ["source_import_id"], ["import_batches.id"], name="fk_medications_source_import_id"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_medications_public_id"), "medications", ["public_id"], unique=True)
    op.create_index(
        op.f("ix_medications_workspace_id"), "medications", ["workspace_id"], unique=False
    )
    op.create_index(op.f("ix_medications_user_id"), "medications", ["user_id"], unique=False)

    op.create_table(
        "medication_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("medication_id", sa.Integer(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("source_import_id", sa.Integer(), nullable=True),
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
        sa.CheckConstraint("status IN ('taken', 'skipped')", name="ck_medication_events_status"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_medication_events_workspace"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_medication_events_user"),
        sa.ForeignKeyConstraint(
            ["medication_id"],
            ["medications.id"],
            name="fk_medication_events_medication",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_import_id"],
            ["import_batches.id"],
            name="fk_medication_events_source_import_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("medication_id", "scheduled_for", name="uq_medication_events_slot"),
    )
    op.create_index(
        op.f("ix_medication_events_public_id"), "medication_events", ["public_id"], unique=True
    )
    op.create_index(
        op.f("ix_medication_events_workspace_id"),
        "medication_events",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_medication_events_medication_id"),
        "medication_events",
        ["medication_id"],
        unique=False,
    )

    op.create_table(
        "weight_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("weight_kg", sa.Numeric(precision=6, scale=2), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("source_import_id", sa.Integer(), nullable=True),
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
        sa.CheckConstraint("weight_kg > 0", name="ck_weight_entries_positive"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_weight_entries_workspace"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_weight_entries_user"),
        sa.ForeignKeyConstraint(
            ["source_import_id"],
            ["import_batches.id"],
            name="fk_weight_entries_source_import_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_weight_entries_public_id"), "weight_entries", ["public_id"], unique=True
    )
    op.create_index(
        "ix_weight_entries_workspace_id_measured_at",
        "weight_entries",
        ["workspace_id", "measured_at"],
        unique=False,
    )

    # health_summary on weekly_summaries (spec-069 §C) — additive JSON column,
    # same convention as todo_summary/spending_summary.
    op.add_column("weekly_summaries", sa.Column("health_summary", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("weekly_summaries", "health_summary")
    op.drop_index("ix_weight_entries_workspace_id_measured_at", table_name="weight_entries")
    op.drop_index(op.f("ix_weight_entries_public_id"), table_name="weight_entries")
    op.drop_table("weight_entries")
    op.drop_index(op.f("ix_medication_events_medication_id"), table_name="medication_events")
    op.drop_index(op.f("ix_medication_events_workspace_id"), table_name="medication_events")
    op.drop_index(op.f("ix_medication_events_public_id"), table_name="medication_events")
    op.drop_table("medication_events")
    op.drop_index(op.f("ix_medications_user_id"), table_name="medications")
    op.drop_index(op.f("ix_medications_workspace_id"), table_name="medications")
    op.drop_index(op.f("ix_medications_public_id"), table_name="medications")
    op.drop_table("medications")
