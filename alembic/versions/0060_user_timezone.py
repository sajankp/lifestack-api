"""add reusable user timezone preference (web spec-011)

Revision ID: 0060_user_timezone
Revises: 0059_medication_schedule_mode
Create Date: 2026-08-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0060_user_timezone"
down_revision = "0059_medication_schedule_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Null means the web client uses its browser-detected IANA timezone until
    # the user explicitly persists a preference.
    op.add_column("users", sa.Column("timezone", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "timezone")
