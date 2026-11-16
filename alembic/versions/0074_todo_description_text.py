"""todo and recurring-rule descriptions become TEXT (API limit 2000).

Widens ``todos.description`` and ``recurring_todo_rules.description`` from
VARCHAR(500) to TEXT. The length limit now lives only in the Pydantic schemas
(2000), so it can change without another migration. Postgres stores both types
identically and widening to TEXT does not rewrite the table.

The downgrade narrows back to VARCHAR(500) and fails loudly if any row is longer;
it never truncates data.
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0074_todo_description_text"
down_revision: str | None = "0073_stale_price_days"
branch_labels: str | None = None
depends_on: str | None = None

_TABLES = ("todos", "recurring_todo_rules")


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table,
            "description",
            existing_type=sa.String(length=500),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table,
            "description",
            existing_type=sa.Text(),
            type_=sa.String(length=500),
            existing_nullable=True,
        )
