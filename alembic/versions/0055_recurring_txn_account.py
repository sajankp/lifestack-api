"""add account_id to recurring_transactions (spec-084)

Recurring transactions had no account reference at all, so every generated
spending transaction silently landed with a NULL account regardless of the
workspace default-account setting (spec-054's invariant reopened for the
recurring path). Adds a nullable account_id FK, tenant-scoped like every
other account reference in this schema (accounts.id + accounts.workspace_id,
reusing the uq_accounts_id_workspace constraint from 0022).

Forward-only: existing rows get account_id = NULL and are not backfilled.

Revision ID: 0055_recurring_txn_account
Revises: 0054_weekly_summary_cadence
Create Date: 2026-07-17 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0055_recurring_txn_account"
down_revision = "0054_weekly_summary_cadence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recurring_transactions",
        sa.Column("account_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_recurring_transactions_account_id",
        "recurring_transactions",
        ["account_id"],
    )
    op.create_foreign_key(
        "fk_recurring_transactions_account_workspace",
        "recurring_transactions",
        "accounts",
        ["account_id", "workspace_id"],
        ["id", "workspace_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_recurring_transactions_account_workspace",
        "recurring_transactions",
        type_="foreignkey",
    )
    op.drop_index("ix_recurring_transactions_account_id", table_name="recurring_transactions")
    op.drop_column("recurring_transactions", "account_id")
