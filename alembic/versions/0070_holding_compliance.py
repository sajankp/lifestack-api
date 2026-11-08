"""add halal compliance verdicts per holding.

Screeners disagree, so every verdict carries the sources that produced it
(screener name, URL, rule reference, optional per-source verdict and date)
and an as-of date. One current row per holding; history lives in the audit
log. ``impure_income_pct`` (0-100) is the screener-stated share of
non-permissible income and the only input to purification estimates.

Revision ID: 0070_holding_compliance
Revises: 0068_capital_gains_tax
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0070_holding_compliance"
down_revision: str | None = "0069_company_industry"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "investing_holding_compliance",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("holding_id", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("impure_income_pct", sa.Numeric(precision=7, scale=4), nullable=True),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_investing_holding_compliance_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["holding_id"],
            ["investing_holdings.id"],
            name="fk_investing_holding_compliance_holding",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "verdict IN ('halal', 'non_halal', 'permissible', 'unknown')",
            name="ck_investing_holding_compliance_verdict",
        ),
        sa.CheckConstraint(
            "impure_income_pct IS NULL OR (impure_income_pct >= 0 AND impure_income_pct <= 100)",
            name="ck_investing_holding_compliance_pct_range",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "holding_id",
            name="uq_investing_holding_compliance_workspace_holding",
        ),
    )
    op.create_index(
        op.f("ix_investing_holding_compliance_public_id"),
        "investing_holding_compliance",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_investing_holding_compliance_holding_id"),
        "investing_holding_compliance",
        ["holding_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_investing_holding_compliance_holding_id"),
        table_name="investing_holding_compliance",
    )
    op.drop_index(
        op.f("ix_investing_holding_compliance_public_id"),
        table_name="investing_holding_compliance",
    )
    op.drop_table("investing_holding_compliance")
