"""refresh_token_rotation

Revision ID: b57cfd2fe215
Revises: 48f0b6946826
Create Date: 2026-06-05 16:53:04.344917
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "b57cfd2fe215"
down_revision = "48f0b6946826"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "auth_sessions", sa.Column("current_token_hash", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "auth_sessions", sa.Column("previous_token_hash", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "auth_sessions", sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("auth_sessions", "rotated_at")
    op.drop_column("auth_sessions", "previous_token_hash")
    op.drop_column("auth_sessions", "current_token_hash")
