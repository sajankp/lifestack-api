"""add commit_error to import_batches

Revision ID: 0034_import_batch_commit_error
Revises: 0033_add_investing_orders
Create Date: 2026-06-28 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0034_import_batch_commit_error"
down_revision = "0033_add_investing_orders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "import_batches",
        sa.Column("commit_error", sa.String(length=2048), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("import_batches", "commit_error")
