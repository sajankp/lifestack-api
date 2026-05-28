"""add wallet_name and labels to spending transactions

Revision ID: 0016_wallet_labels_tx
Revises: 0015_create_import_batches
Create Date: 2026-05-28 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016_wallet_labels_tx"
down_revision: str | None = "0015_create_import_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "spending_transactions",
        sa.Column("wallet_name", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "spending_transactions",
        sa.Column("labels", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("spending_transactions", "labels")
    op.drop_column("spending_transactions", "wallet_name")
