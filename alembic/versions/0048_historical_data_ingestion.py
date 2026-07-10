"""historical data ingestion: user FX rates + net-worth backfill points (spec-072)

Two independent changes:
A. fx_rates gains a nullable workspace_id (NULL = system/global, existing
   rows untouched). A user-provided historical rate is a row scoped to one
   workspace; a partial unique index enforces the upsert key for those rows
   only. System-rate queries used by live valuation must filter
   workspace_id IS NULL (enforced in the repository layer, not schema).
B. net_worth_snapshots gains `source` ('live' default, 'user_provided' for
   backfill) and the three component columns become nullable — a bare-total
   user point has no components. A CHECK guards that 'live' rows (the only
   kind the daily job ever writes) always populate all three, so the
   nullability only ever applies to user-provided rows.

Revision ID: 0048_historical_data_ingestion
Revises: 0047_investing_dividends
Create Date: 2026-07-11 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0048_historical_data_ingestion"
down_revision = "0047_investing_dividends"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A. Historical FX
    op.add_column("fx_rates", sa.Column("workspace_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_fx_rates_workspace",
        "fx_rates",
        "workspaces",
        ["workspace_id"],
        ["id"],
    )
    op.create_index("ix_fx_rates_workspace_id", "fx_rates", ["workspace_id"])
    op.create_index(
        "uq_fx_rate_user_row",
        "fx_rates",
        ["workspace_id", "base_currency_code", "quote_currency_code", "as_of"],
        unique=True,
        postgresql_where=sa.text("workspace_id IS NOT NULL"),
    )

    # B. Net-worth backfill points
    op.add_column(
        "net_worth_snapshots",
        sa.Column("source", sa.String(length=20), nullable=False, server_default="live"),
    )
    op.alter_column("net_worth_snapshots", "holdings_value", nullable=True)
    op.alter_column("net_worth_snapshots", "investing_cash", nullable=True)
    op.alter_column("net_worth_snapshots", "spending_cash", nullable=True)
    op.create_check_constraint(
        "ck_net_worth_snapshots_source",
        "net_worth_snapshots",
        "source IN ('live', 'user_provided')",
    )
    op.create_check_constraint(
        "ck_net_worth_snapshots_live_components_complete",
        "net_worth_snapshots",
        "(source != 'live') OR "
        "(holdings_value IS NOT NULL AND investing_cash IS NOT NULL AND spending_cash IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_net_worth_snapshots_live_components_complete", "net_worth_snapshots", type_="check"
    )
    op.drop_constraint("ck_net_worth_snapshots_source", "net_worth_snapshots", type_="check")
    # Reversibility of user data (spec-072 INV-5): drop user-provided rows
    # before restoring NOT NULL, since they may carry NULL components.
    op.execute("DELETE FROM net_worth_snapshots WHERE source = 'user_provided'")
    op.alter_column("net_worth_snapshots", "spending_cash", nullable=False)
    op.alter_column("net_worth_snapshots", "investing_cash", nullable=False)
    op.alter_column("net_worth_snapshots", "holdings_value", nullable=False)
    op.drop_column("net_worth_snapshots", "source")

    op.drop_index("uq_fx_rate_user_row", "fx_rates")
    op.drop_index("ix_fx_rates_workspace_id", "fx_rates")
    op.drop_constraint("fk_fx_rates_workspace", "fx_rates", type_="foreignkey")
    op.drop_column("fx_rates", "workspace_id")
