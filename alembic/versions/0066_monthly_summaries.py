"""create monthly_summaries table for periodic monthly financial close (spec-096)."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0066_monthly_summaries"
down_revision: str | None = "0065_mcp_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monthly_summaries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("month_start", sa.Date(), nullable=False),
        sa.Column("month_end", sa.Date(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("todo_summary", sa.JSON(), nullable=False),
        sa.Column("spending_summary", sa.JSON(), nullable=False),
        sa.Column("investing_summary", sa.JSON(), nullable=False),
        sa.Column("health_summary", sa.JSON(), nullable=True),
        sa.Column("dividend_summary", sa.JSON(), nullable=True),
        sa.Column("net_worth_summary", sa.JSON(), nullable=True),
        sa.Column("return_metrics_summary", sa.JSON(), nullable=True),
        sa.Column("highlights", sa.JSON(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by_id", sa.Integer(), nullable=True),
        sa.Column("regenerated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("regeneration_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("id", "workspace_id", name="uq_monthly_summaries_id_workspace"),
        sa.ForeignKeyConstraint(
            ["superseded_by_id", "workspace_id"],
            ["monthly_summaries.id", "monthly_summaries.workspace_id"],
            name="fk_monthly_summaries_superseded_by",
        ),
    )
    op.create_index(
        "ix_monthly_summaries_public_id", "monthly_summaries", ["public_id"], unique=True
    )
    op.create_index("ix_monthly_summaries_workspace_id", "monthly_summaries", ["workspace_id"])
    op.create_index(
        "ix_monthly_summaries_superseded_by_id",
        "monthly_summaries",
        ["superseded_by_id"],
    )
    op.create_index(
        "uq_monthly_summary_workspace_month_current",
        "monthly_summaries",
        ["workspace_id", "month_start"],
        unique=True,
        postgresql_where=sa.text("superseded_by_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_monthly_summary_workspace_month_current", table_name="monthly_summaries")
    op.drop_index("ix_monthly_summaries_superseded_by_id", table_name="monthly_summaries")
    op.drop_index("ix_monthly_summaries_workspace_id", table_name="monthly_summaries")
    op.drop_index("ix_monthly_summaries_public_id", table_name="monthly_summaries")
    op.drop_table("monthly_summaries")
