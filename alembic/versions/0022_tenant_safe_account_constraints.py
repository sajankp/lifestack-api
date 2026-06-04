"""add tenant-safe account reference constraints

Revision ID: 0022_tenant_account_fk
Revises: 0021_spending_tx_source
Create Date: 2026-06-04 12:20:00.000000
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022_tenant_account_fk"
down_revision: str | None = "0021_spending_tx_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_accounts_id_workspace",
        "accounts",
        ["id", "workspace_id"],
    )
    op.create_foreign_key(
        "fk_spending_transactions_account_workspace",
        "spending_transactions",
        "accounts",
        ["account_id", "workspace_id"],
        ["id", "workspace_id"],
    )
    op.create_foreign_key(
        "fk_capital_transfers_from_account_workspace",
        "capital_transfers",
        "accounts",
        ["from_account_id", "workspace_id"],
        ["id", "workspace_id"],
    )
    op.create_foreign_key(
        "fk_capital_transfers_to_account_workspace",
        "capital_transfers",
        "accounts",
        ["to_account_id", "workspace_id"],
        ["id", "workspace_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_capital_transfers_to_account_workspace",
        "capital_transfers",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_capital_transfers_from_account_workspace",
        "capital_transfers",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_spending_transactions_account_workspace",
        "spending_transactions",
        type_="foreignkey",
    )
    op.drop_constraint("uq_accounts_id_workspace", "accounts", type_="unique")
