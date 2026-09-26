"""add credit_account_id to investing_dividends (spec-097).

Allows dividends to credit a different account than the holding's brokerage
account — e.g. Indian-market dividends that land in a linked bank account.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0067_dividend_credit_account"
down_revision: str | None = "0066_monthly_summaries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "investing_dividends",
        sa.Column("credit_account_id", sa.Integer(), nullable=True),
    )
    # FK to accounts(id, workspace_id) — composite foreign key matching
    # the existing pattern used by the table's own account_id FK.
    op.create_foreign_key(
        "fk_investing_dividends_credit_account_workspace",
        "investing_dividends",
        "accounts",
        ["credit_account_id", "workspace_id"],
        ["id", "workspace_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_investing_dividends_credit_account_workspace",
        "investing_dividends",
        type_="foreignkey",
    )
    op.drop_column("investing_dividends", "credit_account_id")
