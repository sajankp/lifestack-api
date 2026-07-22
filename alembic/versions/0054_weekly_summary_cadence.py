"""weekly summary cadence + regeneration (spec-076)

Adds the regeneration trail to weekly_summaries (superseded_by_id,
regenerated_at, regeneration_reason) plus a composite (id, workspace_id)
unique so the self-FK stays workspace-scoped like every other cross-row
reference in this schema. Also adds workspace_summary_settings, a
singleton-per-workspace table (same shape as workspace_finance_settings)
holding the per-workspace weekly cadence (day-of-week + hour, UTC).

Also replaces the original (0012) plain unique constraint on
(workspace_id, week_start) with a partial unique index scoped to
superseded_by_id IS NULL: regeneration retains the old row for a week
rather than deleting it, so more than one row per (workspace, week) is now
valid as long as only one of them is the current (non-superseded) version.

Forward-only: existing weekly_summaries rows get superseded_by_id/
regenerated_at/regeneration_reason = NULL (never regenerated); no backfill.
Workspaces get no workspace_summary_settings row until they set one — the
job falls back to the prior global Monday 01:30 UTC default when a
workspace has no row.

Revision ID: 0054_weekly_summary_cadence
Revises: 0053_reference_securities
Create Date: 2026-07-16 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0054_weekly_summary_cadence"
down_revision = "0053_reference_securities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_weekly_summary_workspace_week", "weekly_summaries", type_="unique")
    op.add_column(
        "weekly_summaries",
        sa.Column("superseded_by_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "weekly_summaries",
        sa.Column("regenerated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "weekly_summaries",
        sa.Column("regeneration_reason", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "weekly_summaries",
        sa.Column("dividend_summary", sa.JSON(), nullable=True),
    )
    op.add_column(
        "weekly_summaries",
        sa.Column("net_worth_summary", sa.JSON(), nullable=True),
    )
    op.add_column(
        "weekly_summaries",
        sa.Column("return_metrics_summary", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_weekly_summaries_superseded_by_id",
        "weekly_summaries",
        ["superseded_by_id"],
    )
    op.create_unique_constraint(
        "uq_weekly_summaries_id_workspace",
        "weekly_summaries",
        ["id", "workspace_id"],
    )
    op.create_foreign_key(
        "fk_weekly_summaries_superseded_by",
        "weekly_summaries",
        "weekly_summaries",
        ["superseded_by_id", "workspace_id"],
        ["id", "workspace_id"],
    )
    op.create_index(
        "uq_weekly_summary_workspace_week_current",
        "weekly_summaries",
        ["workspace_id", "week_start"],
        unique=True,
        postgresql_where=sa.text("superseded_by_id IS NULL"),
    )

    op.create_table(
        "workspace_summary_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("cadence_day_of_week", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cadence_hour_utc", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.UniqueConstraint("workspace_id", name="uq_workspace_summary_settings_workspace"),
    )


def downgrade() -> None:
    op.drop_table("workspace_summary_settings")
    op.drop_index("uq_weekly_summary_workspace_week_current", table_name="weekly_summaries")
    op.drop_constraint("fk_weekly_summaries_superseded_by", "weekly_summaries", type_="foreignkey")
    op.drop_constraint("uq_weekly_summaries_id_workspace", "weekly_summaries", type_="unique")
    op.drop_index("ix_weekly_summaries_superseded_by_id", table_name="weekly_summaries")
    op.drop_column("weekly_summaries", "return_metrics_summary")
    op.drop_column("weekly_summaries", "net_worth_summary")
    op.drop_column("weekly_summaries", "dividend_summary")
    op.drop_column("weekly_summaries", "regeneration_reason")
    op.drop_column("weekly_summaries", "regenerated_at")
    op.drop_column("weekly_summaries", "superseded_by_id")
    op.create_unique_constraint(
        "uq_weekly_summary_workspace_week", "weekly_summaries", ["workspace_id", "week_start"]
    )
