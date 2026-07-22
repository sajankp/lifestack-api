"""add composite indexes for high-frequency query patterns (perf optimization)

Revision ID: 0056_composite_indexes_perf
Revises: 0055_recurring_txn_account
Create Date: 2026-07-18 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0056_composite_indexes_perf"
down_revision = "0055_recurring_txn_account"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # spending_transactions(workspace_id, user_id, occurred_at DESC)
    # Optimizes: list transactions by workspace/user ordered by date (dashboard, spending lists)
    op.create_index(
        "ix_spending_transactions_workspace_user_occurred_at_desc",
        "spending_transactions",
        ["workspace_id", "user_id", sa.text("occurred_at DESC")],
    )

    # investing_holdings(workspace_id, account_id, instrument_id)
    # Optimizes: portfolio views, holding lookups by account and instrument
    op.create_index(
        "ix_investing_holdings_workspace_account_instrument",
        "investing_holdings",
        ["workspace_id", "account_id", "instrument_id"],
    )

    # auth_sessions(user_id, revoked_at, expires_at)
    # Optimizes: session cleanup job, active session lookups
    op.create_index(
        "ix_auth_sessions_user_revoked_expires",
        "auth_sessions",
        ["user_id", "revoked_at", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_auth_sessions_user_revoked_expires", table_name="auth_sessions")
    op.drop_index(
        "ix_investing_holdings_workspace_account_instrument", table_name="investing_holdings"
    )
    op.drop_index(
        "ix_spending_transactions_workspace_user_occurred_at_desc",
        table_name="spending_transactions",
    )
