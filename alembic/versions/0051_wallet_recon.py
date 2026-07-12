"""add account_statements + statement_lines tables (spec-078)

Wallet ledger reconciliation (statement matching). Matching is metadata,
never mutation (INV-1): these two tables never cause a write to
spending_transactions/capital_transfers, and investing_cash_balances is
untouched (INV-2). closing_balance on account_statements is a reference
value only, never a snapshot row.

Revision ID: 0051_wallet_recon
Revises: 0050_financial_kpis
Create Date: 2026-07-12 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0051_wallet_recon"
down_revision = "0050_financial_kpis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_statements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("closing_balance", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("currency_code", sa.String(length=10), nullable=False),
        sa.Column("import_batch_id", sa.Integer(), nullable=True),
        sa.Column("reconciled_through", sa.Date(), nullable=True),
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
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["currency_code"], ["currencies.code"]),
        sa.ForeignKeyConstraint(["import_batch_id"], ["import_batches.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_account_statements_id_workspace"),
    )
    op.create_index("ix_account_statements_workspace_id", "account_statements", ["workspace_id"])
    op.create_index("ix_account_statements_account_id", "account_statements", ["account_id"])
    op.create_index(
        "ix_account_statements_import_batch_id", "account_statements", ["import_batch_id"]
    )

    op.create_table(
        "statement_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("statement_id", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.Date(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("balance", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("external_ref", sa.String(length=64), nullable=False),
        sa.Column("matched_transaction_id", sa.Integer(), nullable=True),
        sa.Column("matched_transfer_id", sa.Integer(), nullable=True),
        sa.Column("matched_transfer_leg", sa.String(length=10), nullable=True),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["statement_id"], ["account_statements.id"]),
        sa.ForeignKeyConstraint(["matched_transaction_id"], ["spending_transactions.id"]),
        sa.ForeignKeyConstraint(["matched_transfer_id"], ["capital_transfers.id"]),
        sa.CheckConstraint(
            "NOT (matched_transaction_id IS NOT NULL AND matched_transfer_id IS NOT NULL)",
            name="ck_statement_lines_exactly_one_match_target",
        ),
        sa.CheckConstraint(
            "(matched_transfer_leg IS NULL) OR (matched_transfer_id IS NOT NULL)",
            name="ck_statement_lines_leg_requires_transfer",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint(
            "account_id", "external_ref", name="uq_statement_lines_account_external_ref"
        ),
    )
    op.create_index("ix_statement_lines_workspace_id", "statement_lines", ["workspace_id"])
    op.create_index("ix_statement_lines_account_id", "statement_lines", ["account_id"])
    op.create_index("ix_statement_lines_statement_id", "statement_lines", ["statement_id"])
    op.create_index("ix_statement_lines_occurred_at", "statement_lines", ["occurred_at"])
    op.create_index(
        "ix_statement_lines_matched_transaction_id", "statement_lines", ["matched_transaction_id"]
    )
    op.create_index(
        "ix_statement_lines_matched_transfer_id", "statement_lines", ["matched_transfer_id"]
    )
    op.create_index("ix_statement_lines_external_ref", "statement_lines", ["external_ref"])


def downgrade() -> None:
    op.drop_index("ix_statement_lines_external_ref", "statement_lines")
    op.drop_index("ix_statement_lines_matched_transfer_id", "statement_lines")
    op.drop_index("ix_statement_lines_matched_transaction_id", "statement_lines")
    op.drop_index("ix_statement_lines_occurred_at", "statement_lines")
    op.drop_index("ix_statement_lines_statement_id", "statement_lines")
    op.drop_index("ix_statement_lines_account_id", "statement_lines")
    op.drop_index("ix_statement_lines_workspace_id", "statement_lines")
    op.drop_table("statement_lines")

    op.drop_index("ix_account_statements_import_batch_id", "account_statements")
    op.drop_index("ix_account_statements_account_id", "account_statements")
    op.drop_index("ix_account_statements_workspace_id", "account_statements")
    op.drop_table("account_statements")
