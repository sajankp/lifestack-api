"""portfolio_reports: append-only biweekly report history (spec-102).

One row per ``(workspace_id, as_of, run_id)``; re-runs never overwrite an
existing row. Denormalized dashboard columns:
theme_weights, harvest_candidates, lrs_headroom_inr, tax_buckets.

No changes to existing tables.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0072_portfolio_report_history"
down_revision: str | None = "0071_holding_theme_tags"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False, index=True),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("scalars_json", JSONB(), nullable=True),
        sa.Column("foundation_json", JSONB(), nullable=True),
        sa.Column("actions_md", sa.Text(), nullable=True),
        sa.Column("theme_weights", JSONB(), nullable=True),
        sa.Column("harvest_candidates", JSONB(), nullable=True),
        sa.Column("lrs_headroom_inr", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("tax_buckets", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_portfolio_reports_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_portfolio_reports_public_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "as_of",
            "run_id",
            name="uq_portfolio_reports_workspace_asof_runid",
        ),
    )
    op.create_index(
        "ix_portfolio_reports_workspace_as_of",
        "portfolio_reports",
        ["workspace_id", "as_of"],
    )


def downgrade() -> None:
    op.drop_index("ix_portfolio_reports_workspace_as_of", table_name="portfolio_reports")
    op.drop_table("portfolio_reports")
