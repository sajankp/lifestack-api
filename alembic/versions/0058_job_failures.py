"""add job_failures table (spec-088)

Durable ledger of job/workspace units that exhausted retry or failed outright --
the data source for the daily failure digest and weekly heartbeat. Append-only
by convention (not a delete-blocking trigger like audit_logs): operational data,
retention needs real deletes. Only notified_at / resolved_at are ever updated.

Revision ID: 0058_job_failures
Revises: 0057_composite_indexes_perf_2
Create Date: 2026-07-21 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0058_job_failures"
down_revision = "0057_composite_indexes_perf_2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_failures",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("job_name", sa.String(length=100), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(length=200), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index("ix_job_failures_job_name", "job_failures", ["job_name"])
    op.create_index("ix_job_failures_workspace_id", "job_failures", ["workspace_id"])
    op.create_index("ix_job_failures_created_at", "job_failures", ["created_at"])
    op.create_index("ix_job_failures_resolved_at", "job_failures", ["resolved_at"])


def downgrade() -> None:
    op.drop_index("ix_job_failures_resolved_at", table_name="job_failures")
    op.drop_index("ix_job_failures_created_at", table_name="job_failures")
    op.drop_index("ix_job_failures_workspace_id", table_name="job_failures")
    op.drop_index("ix_job_failures_job_name", table_name="job_failures")
    op.drop_table("job_failures")
