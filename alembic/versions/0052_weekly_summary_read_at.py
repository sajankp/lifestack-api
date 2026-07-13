"""add read_at to weekly_summaries (spec-080)

Weekly-summary read state: a nullable timestamp recording when the user first
opened a summary, so the "summary is ready" morning-briefing line clears once
read (instead of persisting for the full freshness window). Forward-only —
existing rows are NULL (unread) and age out of the window as before; no backfill.

Revision ID: 0052_weekly_summary_read_at
Revises: 0051_wallet_recon
Create Date: 2026-07-13 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0052_weekly_summary_read_at"
down_revision = "0051_wallet_recon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "weekly_summaries",
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("weekly_summaries", "read_at")
