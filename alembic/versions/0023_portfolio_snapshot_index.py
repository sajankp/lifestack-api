"""add portfolio snapshot latest-query index

Revision ID: 0023_snapshot_index
Revises: 0022_tenant_account_fk
Create Date: 2026-06-05 09:25:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023_snapshot_index"
down_revision: str | None = "0022_tenant_account_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_portfolio_snapshots_workspace_snapshot_date_desc",
        "portfolio_snapshots",
        ["workspace_id", sa.text("snapshot_date DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_snapshots_workspace_snapshot_date_desc",
        table_name="portfolio_snapshots",
    )
