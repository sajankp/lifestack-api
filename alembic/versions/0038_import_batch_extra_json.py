"""add import_batches.extra_json (spec-056: CAMS CAS import)

Generic advisory-only preview metadata column. First user: CAMS CAS import
stores {"target_account_id": ...} (the single account every parsed
transaction is bound to) plus, after validation, skipped-row and
price-discontinuity-warning lists. Nullable, no backfill — every other
module leaves it null.

Revision ID: 0038_import_batch_extra_json
Revises: 0037_corporate_actions
Create Date: 2026-07-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0038_import_batch_extra_json"
down_revision = "0037_corporate_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "import_batches",
        sa.Column("extra_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("import_batches", "extra_json")
