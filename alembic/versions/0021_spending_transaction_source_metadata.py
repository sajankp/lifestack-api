"""add spending transaction source metadata

Revision ID: 0021_spending_tx_source
Revises: 0020_fx_rate_check_constraint
Create Date: 2026-06-04 13:12:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021_spending_tx_source"
down_revision: str | None = "0020_fx_rate_check_constraint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "spending_transactions",
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default="manual"),
    )
    op.add_column("spending_transactions", sa.Column("source_ref", sa.String(length=255)))
    op.add_column("spending_transactions", sa.Column("source_import_id", sa.Integer()))
    op.create_index(
        "ix_spending_transactions_source_type",
        "spending_transactions",
        ["source_type"],
    )
    op.create_index(
        "ix_spending_transactions_source_import_id",
        "spending_transactions",
        ["source_import_id"],
    )
    op.create_foreign_key(
        "fk_spending_transactions_source_import",
        "spending_transactions",
        "import_batches",
        ["source_import_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_spending_transactions_source_import",
        "spending_transactions",
        type_="foreignkey",
    )
    op.drop_index("ix_spending_transactions_source_import_id", table_name="spending_transactions")
    op.drop_index("ix_spending_transactions_source_type", table_name="spending_transactions")
    op.drop_column("spending_transactions", "source_import_id")
    op.drop_column("spending_transactions", "source_ref")
    op.drop_column("spending_transactions", "source_type")
