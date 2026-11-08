"""add industry to investing_companies (spec-100).

Companies track a sector (already present) and now an industry, both free text
up to 100 characters. GICS sector names are recommended but not enforced.
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0069_company_industry"
down_revision: str | None = "0068_capital_gains_tax"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "investing_companies",
        sa.Column("industry", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("investing_companies", "industry")
