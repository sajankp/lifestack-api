"""medication schedule_mode + event taken_at (spec-092)

Revision ID: 0059_medication_schedule_mode
Revises: 0058_job_failures
Create Date: 2026-07-24 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0059_medication_schedule_mode"
down_revision = "0058_job_failures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Opt-in re-anchoring mode; every existing medication becomes 'fixed'.
    op.add_column(
        "medications",
        sa.Column(
            "schedule_mode",
            sa.String(length=24),
            nullable=False,
            server_default="fixed",
        ),
    )
    op.create_check_constraint(
        "ck_medications_schedule_mode",
        "medications",
        "schedule_mode IN ('fixed', 'interval_from_last_dose')",
    )

    # Actual-intake moment for a dose event (nullable; set for 'taken' only).
    op.add_column(
        "medication_events",
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill existing 'taken' rows so interval scheduling and adherence display
    # have a sane intake time (the adherence log is derived, not an append-only
    # snapshot/ledger, so this in-migration backfill is allowed — spec-092 §A).
    op.execute("UPDATE medication_events SET taken_at = logged_at WHERE status = 'taken'")


def downgrade() -> None:
    op.drop_column("medication_events", "taken_at")
    op.drop_constraint("ck_medications_schedule_mode", "medications", type_="check")
    op.drop_column("medications", "schedule_mode")
