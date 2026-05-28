"""add account link to spending transactions

Revision ID: 0017_spending_tx_account
Revises: 0016_wallet_labels_tx
Create Date: 2026-05-28 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017_spending_tx_account"
down_revision: str | None = "0016_wallet_labels_tx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "spending_transactions",
        sa.Column("account_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_spending_transactions_account_id"),
        "spending_transactions",
        ["account_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_spending_transactions_account_id",
        "spending_transactions",
        "accounts",
        ["account_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_spending_transactions_account_id", "spending_transactions", type_="foreignkey"
    )
    op.drop_index(op.f("ix_spending_transactions_account_id"), table_name="spending_transactions")
    op.drop_column("spending_transactions", "account_id")
