"""investing lookthrough analytics foundation

Revision ID: 0010_investing_lookthrough
Revises: 0009_fx_rates_transfers
Create Date: 2026-05-24 12:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
import sqlmodel

from alembic import op

# revision identifiers, used by Alembic.
revision = "0010_investing_lookthrough"
down_revision = "0009_fx_rates_transfers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "investing_companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("ticker", sqlmodel.sql.sqltypes.AutoString(length=20), nullable=True),
        sa.Column("isin", sqlmodel.sql.sqltypes.AutoString(length=20), nullable=True),
        sa.Column("sector", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=True),
        sa.Column("country_code", sqlmodel.sql.sqltypes.AutoString(length=10), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_investing_company_workspace_name"),
    )
    op.create_index(
        op.f("ix_investing_companies_public_id"), "investing_companies", ["public_id"], unique=True
    )
    op.create_index(
        op.f("ix_investing_companies_workspace_id"),
        "investing_companies",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_investing_companies_ticker"), "investing_companies", ["ticker"], unique=False
    )

    op.create_table(
        "investing_instruments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column(
            "instrument_type",
            sa.Enum("stock", "etf", "mutual_fund", name="instrument_type"),
            nullable=False,
            server_default="stock",
        ),
        sa.Column("isin", sqlmodel.sql.sqltypes.AutoString(length=20), nullable=True),
        sa.Column("exchange", sqlmodel.sql.sqltypes.AutoString(length=50), nullable=True),
        sa.Column("provider_key", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=True),
        sa.Column("company_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["investing_companies.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "symbol",
            name="uq_investing_instrument_workspace_symbol",
        ),
    )
    op.create_index(
        op.f("ix_investing_instruments_public_id"),
        "investing_instruments",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_investing_instruments_workspace_id"),
        "investing_instruments",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_investing_instruments_company_id"),
        "investing_instruments",
        ["company_id"],
        unique=False,
    )

    op.create_table(
        "investing_instrument_constituents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("constituent_company_id", sa.Integer(), nullable=False),
        sa.Column("weight", sa.Numeric(precision=10, scale=8), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("source", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["constituent_company_id"], ["investing_companies.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["investing_instruments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instrument_id",
            "constituent_company_id",
            "as_of_date",
            "source",
            name="uq_investing_constituent_snapshot",
        ),
    )
    op.create_index(
        op.f("ix_investing_instrument_constituents_instrument_id"),
        "investing_instrument_constituents",
        ["instrument_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_investing_instrument_constituents_constituent_company_id"),
        "investing_instrument_constituents",
        ["constituent_company_id"],
        unique=False,
    )

    op.add_column("investing_holdings", sa.Column("instrument_id", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_investing_holdings_instrument_id"),
        "investing_holdings",
        ["instrument_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_investing_holdings_instrument_id",
        "investing_holdings",
        "investing_instruments",
        ["instrument_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_investing_holdings_instrument_id", "investing_holdings", type_="foreignkey"
    )
    op.drop_index(op.f("ix_investing_holdings_instrument_id"), table_name="investing_holdings")
    op.drop_column("investing_holdings", "instrument_id")

    op.drop_index(
        op.f("ix_investing_instrument_constituents_constituent_company_id"),
        table_name="investing_instrument_constituents",
    )
    op.drop_index(
        op.f("ix_investing_instrument_constituents_instrument_id"),
        table_name="investing_instrument_constituents",
    )
    op.drop_table("investing_instrument_constituents")

    op.drop_index(op.f("ix_investing_instruments_company_id"), table_name="investing_instruments")
    op.drop_index(op.f("ix_investing_instruments_workspace_id"), table_name="investing_instruments")
    op.drop_index(op.f("ix_investing_instruments_public_id"), table_name="investing_instruments")
    op.drop_table("investing_instruments")
    op.execute("DROP TYPE instrument_type")

    op.drop_index(op.f("ix_investing_companies_ticker"), table_name="investing_companies")
    op.drop_index(op.f("ix_investing_companies_workspace_id"), table_name="investing_companies")
    op.drop_index(op.f("ix_investing_companies_public_id"), table_name="investing_companies")
    op.drop_table("investing_companies")
