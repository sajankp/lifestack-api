"""hybrid_instrument_catalog

Revision ID: 8d6e117cb0b0
Revises: 0031_lookthrough_threshold
Create Date: 2026-06-24 09:57:14.290220
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "8d6e117cb0b0"
down_revision = "0031_lookthrough_threshold"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Alter investing_companies.workspace_id to be nullable
    op.alter_column(
        "investing_companies", "workspace_id", existing_type=sa.INTEGER(), nullable=True
    )
    op.drop_constraint("uq_investing_company_workspace_name", "investing_companies", type_="unique")
    op.create_index(
        "uq_global_company_name",
        "investing_companies",
        ["name"],
        unique=True,
        postgresql_where=sa.text("workspace_id IS NULL"),
    )
    op.create_index(
        "uq_workspace_company_name",
        "investing_companies",
        ["workspace_id", "name"],
        unique=True,
        postgresql_where=sa.text("workspace_id IS NOT NULL"),
    )

    # Alter investing_instruments.workspace_id to be nullable
    op.alter_column(
        "investing_instruments", "workspace_id", existing_type=sa.INTEGER(), nullable=True
    )
    op.drop_constraint(
        "uq_investing_instrument_workspace_symbol", "investing_instruments", type_="unique"
    )
    op.create_index(
        "uq_global_instrument_symbol",
        "investing_instruments",
        ["symbol"],
        unique=True,
        postgresql_where=sa.text("workspace_id IS NULL"),
    )
    op.create_index(
        "uq_workspace_instrument_symbol",
        "investing_instruments",
        ["workspace_id", "symbol"],
        unique=True,
        postgresql_where=sa.text("workspace_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_workspace_instrument_symbol",
        table_name="investing_instruments",
        postgresql_where=sa.text("workspace_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_global_instrument_symbol",
        table_name="investing_instruments",
        postgresql_where=sa.text("workspace_id IS NULL"),
    )
    op.create_unique_constraint(
        "uq_investing_instrument_workspace_symbol",
        "investing_instruments",
        ["workspace_id", "symbol"],
    )
    op.alter_column(
        "investing_instruments", "workspace_id", existing_type=sa.INTEGER(), nullable=False
    )

    op.drop_index(
        "uq_workspace_company_name",
        table_name="investing_companies",
        postgresql_where=sa.text("workspace_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_global_company_name",
        table_name="investing_companies",
        postgresql_where=sa.text("workspace_id IS NULL"),
    )
    op.create_unique_constraint(
        "uq_investing_company_workspace_name", "investing_companies", ["workspace_id", "name"]
    )
    op.alter_column(
        "investing_companies", "workspace_id", existing_type=sa.INTEGER(), nullable=False
    )
