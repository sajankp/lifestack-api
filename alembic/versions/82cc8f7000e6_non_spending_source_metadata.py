"""non_spending_source_metadata

Revision ID: 82cc8f7000e6
Revises: 0024_investing_account_migration
Create Date: 2026-06-05 11:34:02.369634
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "82cc8f7000e6"
down_revision = "0024_investing_account_migration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. capital_transfers
    op.add_column(
        "capital_transfers",
        sa.Column("source_type", sa.String(length=32), server_default="manual", nullable=False),
    )
    op.add_column(
        "capital_transfers", sa.Column("source_ref", sa.String(length=255), nullable=True)
    )
    op.add_column("capital_transfers", sa.Column("source_import_id", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_capital_transfers_source_import_id"),
        "capital_transfers",
        ["source_import_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_capital_transfers_source_type"), "capital_transfers", ["source_type"], unique=False
    )
    op.create_foreign_key(
        "fk_capital_transfers_source_import",
        "capital_transfers",
        "import_batches",
        ["source_import_id"],
        ["id"],
    )

    # 2. investing_cash_balances
    op.add_column(
        "investing_cash_balances",
        sa.Column("source_type", sa.String(length=32), server_default="manual", nullable=False),
    )
    op.add_column(
        "investing_cash_balances", sa.Column("source_ref", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "investing_cash_balances", sa.Column("source_import_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        op.f("ix_investing_cash_balances_source_import_id"),
        "investing_cash_balances",
        ["source_import_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_investing_cash_balances_source_type"),
        "investing_cash_balances",
        ["source_type"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_investing_cash_balances_source_import",
        "investing_cash_balances",
        "import_batches",
        ["source_import_id"],
        ["id"],
    )

    # 3. investing_holdings
    op.add_column(
        "investing_holdings",
        sa.Column("source_type", sa.String(length=32), server_default="manual", nullable=False),
    )
    op.add_column(
        "investing_holdings", sa.Column("source_ref", sa.String(length=255), nullable=True)
    )
    op.add_column("investing_holdings", sa.Column("source_import_id", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_investing_holdings_source_import_id"),
        "investing_holdings",
        ["source_import_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_investing_holdings_source_type"),
        "investing_holdings",
        ["source_type"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_investing_holdings_source_import",
        "investing_holdings",
        "import_batches",
        ["source_import_id"],
        ["id"],
    )

    # 4. spending_budgets
    op.add_column(
        "spending_budgets",
        sa.Column("source_type", sa.String(length=32), server_default="manual", nullable=False),
    )
    op.add_column("spending_budgets", sa.Column("source_ref", sa.String(length=255), nullable=True))
    op.add_column("spending_budgets", sa.Column("source_import_id", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_spending_budgets_source_import_id"),
        "spending_budgets",
        ["source_import_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_spending_budgets_source_type"), "spending_budgets", ["source_type"], unique=False
    )
    op.create_foreign_key(
        "fk_spending_budgets_source_import",
        "spending_budgets",
        "import_batches",
        ["source_import_id"],
        ["id"],
    )


def downgrade() -> None:
    # 4. spending_budgets
    op.drop_constraint("fk_spending_budgets_source_import", "spending_budgets", type_="foreignkey")
    op.drop_index(op.f("ix_spending_budgets_source_type"), table_name="spending_budgets")
    op.drop_index(op.f("ix_spending_budgets_source_import_id"), table_name="spending_budgets")
    op.drop_column("spending_budgets", "source_import_id")
    op.drop_column("spending_budgets", "source_ref")
    op.drop_column("spending_budgets", "source_type")

    # 3. investing_holdings
    op.drop_constraint(
        "fk_investing_holdings_source_import", "investing_holdings", type_="foreignkey"
    )
    op.drop_index(op.f("ix_investing_holdings_source_type"), table_name="investing_holdings")
    op.drop_index(op.f("ix_investing_holdings_source_import_id"), table_name="investing_holdings")
    op.drop_column("investing_holdings", "source_import_id")
    op.drop_column("investing_holdings", "source_ref")
    op.drop_column("investing_holdings", "source_type")

    # 2. investing_cash_balances
    op.drop_constraint(
        "fk_investing_cash_balances_source_import", "investing_cash_balances", type_="foreignkey"
    )
    op.drop_index(
        op.f("ix_investing_cash_balances_source_type"), table_name="investing_cash_balances"
    )
    op.drop_index(
        op.f("ix_investing_cash_balances_source_import_id"), table_name="investing_cash_balances"
    )
    op.drop_column("investing_cash_balances", "source_import_id")
    op.drop_column("investing_cash_balances", "source_ref")
    op.drop_column("investing_cash_balances", "source_type")

    # 1. capital_transfers
    op.drop_constraint(
        "fk_capital_transfers_source_import", "capital_transfers", type_="foreignkey"
    )
    op.drop_index(op.f("ix_capital_transfers_source_type"), table_name="capital_transfers")
    op.drop_index(op.f("ix_capital_transfers_source_import_id"), table_name="capital_transfers")
    op.drop_column("capital_transfers", "source_import_id")
    op.drop_column("capital_transfers", "source_ref")
    op.drop_column("capital_transfers", "source_type")
