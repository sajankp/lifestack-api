"""investing_account_migration

Revision ID: 0024_investing_account_migration
Revises: 0023_snapshot_index
Create Date: 2026-06-05 10:01:27.181796
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0024_investing_account_migration"
down_revision = "0023_snapshot_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add account_id as a nullable column first
    op.add_column("investing_holdings", sa.Column("account_id", sa.Integer(), nullable=True))
    op.add_column("investing_cash_balances", sa.Column("account_id", sa.Integer(), nullable=True))

    # 2. Perform transitional backfill of accounts
    connection = op.get_bind()

    # Load all distinct workspace/account_name/currency from holdings
    holdings_rows = connection.execute(
        sa.text("SELECT DISTINCT workspace_id, account_name, currency FROM investing_holdings")
    ).fetchall()

    # Load all distinct workspace/account_name/currency from cash balances
    cash_rows = connection.execute(
        sa.text("SELECT DISTINCT workspace_id, account_name, currency FROM investing_cash_balances")
    ).fetchall()

    # Merge them to find all distinct (workspace_id, account_name) and a candidate currency
    accounts_to_check = {}
    for r in holdings_rows:
        key = (r[0], r[1])
        accounts_to_check[key] = r[2] or "USD"
    for r in cash_rows:
        key = (r[0], r[1])
        if key not in accounts_to_check:
            accounts_to_check[key] = r[2] or "USD"

    # For each required account, find or create it
    for (workspace_id, account_name), currency in accounts_to_check.items():
        # Check if account exists
        existing = connection.execute(
            sa.text("SELECT id FROM accounts WHERE workspace_id = :workspace_id AND name = :name"),
            {"workspace_id": workspace_id, "name": account_name},
        ).fetchone()

        if existing:
            account_id = existing[0]
        else:
            # Create new account
            currency_code = (currency or "USD").upper()
            # Verify currency exists in currencies table
            curr_exists = connection.execute(
                sa.text("SELECT code FROM currencies WHERE code = :code"), {"code": currency_code}
            ).fetchone()
            if not curr_exists:
                currency_code = "USD"

            # Insert workspace currency allowlist
            connection.execute(
                sa.text(
                    "INSERT INTO workspace_currencies (workspace_id, currency_code, created_at) "
                    "VALUES (:workspace_id, :currency_code, :created_at) "
                    "ON CONFLICT (workspace_id, currency_code) DO NOTHING"
                ),
                {
                    "workspace_id": workspace_id,
                    "currency_code": currency_code,
                    "created_at": datetime.now(UTC),
                },
            )

            # Insert account
            account_uuid = str(uuid.uuid4())
            connection.execute(
                sa.text(
                    "INSERT INTO accounts (public_id, workspace_id, name, account_type, default_currency_code, is_active, created_at, updated_at) "
                    "VALUES (:public_id, :workspace_id, :name, :account_type, :default_currency_code, :is_active, :created_at, :updated_at)"
                ),
                {
                    "public_id": account_uuid,
                    "workspace_id": workspace_id,
                    "name": account_name,
                    "account_type": "brokerage",
                    "default_currency_code": currency_code,
                    "is_active": True,
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                },
            )

            # Fetch the generated ID
            created_account = connection.execute(
                sa.text(
                    "SELECT id FROM accounts WHERE workspace_id = :workspace_id AND name = :name"
                ),
                {"workspace_id": workspace_id, "name": account_name},
            ).fetchone()
            account_id = created_account[0]

        # Update holdings with the resolved/created account_id
        connection.execute(
            sa.text(
                "UPDATE investing_holdings SET account_id = :account_id WHERE workspace_id = :workspace_id AND account_name = :account_name"
            ),
            {"account_id": account_id, "workspace_id": workspace_id, "account_name": account_name},
        )

        # Update cash balances with the resolved/created account_id
        connection.execute(
            sa.text(
                "UPDATE investing_cash_balances SET account_id = :account_id WHERE workspace_id = :workspace_id AND account_name = :account_name"
            ),
            {"account_id": account_id, "workspace_id": workspace_id, "account_name": account_name},
        )

    # 3. Alter account_id to be NOT NULL
    op.alter_column("investing_holdings", "account_id", nullable=False)
    op.alter_column("investing_cash_balances", "account_id", nullable=False)

    # 4. Drop the old unique constraint on (workspace_id, symbol, account_name) from holdings
    op.drop_constraint("uq_holding_workspace_symbol_account", "investing_holdings", type_="unique")

    # 5. Create new unique constraint on (workspace_id, symbol, account_id)
    op.create_unique_constraint(
        "uq_holding_workspace_symbol_account",
        "investing_holdings",
        ["workspace_id", "symbol", "account_id"],
    )

    # 6. Create compound foreign key constraints on (account_id, workspace_id) pointing to accounts
    op.create_foreign_key(
        "fk_investing_holdings_account_workspace",
        "investing_holdings",
        "accounts",
        ["account_id", "workspace_id"],
        ["id", "workspace_id"],
    )
    op.create_foreign_key(
        "fk_investing_cash_balances_account_workspace",
        "investing_cash_balances",
        "accounts",
        ["account_id", "workspace_id"],
        ["id", "workspace_id"],
    )

    # 7. Drop the old account_name text columns
    op.drop_column("investing_holdings", "account_name")
    op.drop_column("investing_cash_balances", "account_name")


def downgrade() -> None:
    # 1. Add account_name column as nullable first
    op.add_column(
        "investing_holdings", sa.Column("account_name", sa.String(length=100), nullable=True)
    )
    op.add_column(
        "investing_cash_balances", sa.Column("account_name", sa.String(length=100), nullable=True)
    )

    # 2. Populate account_name back from accounts table
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE investing_holdings SET account_name = (SELECT name FROM accounts WHERE accounts.id = investing_holdings.account_id)"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE investing_cash_balances SET account_name = (SELECT name FROM accounts WHERE accounts.id = investing_cash_balances.account_id)"
        )
    )

    # 3. Alter account_name to be NOT NULL
    op.alter_column("investing_holdings", "account_name", nullable=False)
    op.alter_column("investing_cash_balances", "account_name", nullable=False)

    # 4. Drop the compound foreign keys and new unique constraint
    op.drop_constraint(
        "fk_investing_holdings_account_workspace", "investing_holdings", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_investing_cash_balances_account_workspace",
        "investing_cash_balances",
        type_="foreignkey",
    )
    op.drop_constraint("uq_holding_workspace_symbol_account", "investing_holdings", type_="unique")

    # 5. Re-create the unique constraint on (workspace_id, symbol, account_name)
    op.create_unique_constraint(
        "uq_holding_workspace_symbol_account",
        "investing_holdings",
        ["workspace_id", "symbol", "account_name"],
    )

    # 6. Drop the account_id columns
    op.drop_column("investing_holdings", "account_id")
    op.drop_column("investing_cash_balances", "account_id")
