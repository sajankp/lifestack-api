"""add reference_securities table (spec-083)

Global (workspace_id-less) security-master reference data, following the
FX-rate precedent for global system reference data. Populated by the bundled
`securities.json` loader and enriched on-demand by the Yahoo quote/identity
API-fallback path (never by row-by-row upload-time lookups).

Revision ID: 0053_reference_securities
Revises: 0052_weekly_summary_read_at
Create Date: 2026-07-15 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0053_reference_securities"
down_revision = "0052_weekly_summary_read_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # op.create_table issues CREATE TYPE automatically for named enum columns
    # — no explicit sa.Enum(...).create() pre-create (see migration 0033).
    op.create_table(
        "reference_securities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("isin", sa.String(length=20), nullable=True),
        sa.Column("ticker", sa.String(length=20), nullable=True),
        sa.Column("exchange", sa.String(length=50), nullable=True),
        sa.Column("amfi_code", sa.String(length=20), nullable=True),
        sa.Column(
            "security_type",
            sa.Enum("stock", "etf", "mutual_fund", name="reference_security_type"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("aliases", sa.ARRAY(sa.String()), nullable=False),
        sa.Column("country_code", sa.String(length=10), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_reference_securities_public_id"),
    )
    op.create_index("ix_reference_securities_public_id", "reference_securities", ["public_id"])
    op.create_index("ix_reference_securities_isin", "reference_securities", ["isin"])
    op.create_index("ix_reference_securities_ticker", "reference_securities", ["ticker"])
    op.create_index("ix_reference_securities_amfi_code", "reference_securities", ["amfi_code"])
    op.create_index(
        "uq_reference_securities_isin",
        "reference_securities",
        ["isin"],
        unique=True,
        postgresql_where=sa.text("isin IS NOT NULL"),
    )
    op.create_index(
        "uq_reference_securities_ticker_exchange",
        "reference_securities",
        ["ticker", "exchange"],
        unique=True,
        postgresql_where=sa.text("ticker IS NOT NULL"),
    )
    op.create_index(
        "uq_reference_securities_amfi_code",
        "reference_securities",
        ["amfi_code"],
        unique=True,
        postgresql_where=sa.text("amfi_code IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_reference_securities_amfi_code", "reference_securities")
    op.drop_index("uq_reference_securities_ticker_exchange", "reference_securities")
    op.drop_index("uq_reference_securities_isin", "reference_securities")
    op.drop_index("ix_reference_securities_amfi_code", "reference_securities")
    op.drop_index("ix_reference_securities_ticker", "reference_securities")
    op.drop_index("ix_reference_securities_isin", "reference_securities")
    op.drop_index("ix_reference_securities_public_id", "reference_securities")
    op.drop_table("reference_securities")

    reference_security_type = sa.Enum("stock", "etf", "mutual_fund", name="reference_security_type")
    reference_security_type.drop(op.get_bind(), checkfirst=True)
