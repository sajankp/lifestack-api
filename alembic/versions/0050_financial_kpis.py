"""add financial_kpis table (spec-077)

Custom financial KPI definitions: predefined metric-type enum only in v1
(spend_total/income_total/net_cash_flow), optional filter (category/group/
account), evaluation window, optional target. Single-currency-per-KPI is
enforced at the application layer (create/update AND re-checked at
evaluation time), not the schema — the filter's account set can change
after definition, so a DB constraint can't express it.

Revision ID: 0050_financial_kpis
Revises: 0049_currency_display_profile
Create Date: 2026-07-12 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0050_financial_kpis"
down_revision = "0049_currency_display_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "financial_kpis",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("metric_type", sa.String(length=32), nullable=False),
        sa.Column("evaluation_window", sa.String(length=20), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("category_group_id", sa.Integer(), nullable=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("currency_code", sa.String(length=10), nullable=False),
        sa.Column("target_value", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("target_direction", sa.String(length=4), nullable=True),
        sa.Column("display_format", sa.String(length=20), nullable=False, server_default="amount"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
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
        sa.CheckConstraint(
            "metric_type IN ('spend_total', 'income_total', 'net_cash_flow')",
            name="ck_financial_kpis_metric_type",
        ),
        sa.CheckConstraint(
            "evaluation_window IN ('calendar_month', 'calendar_week', 'rolling_30d')",
            name="ck_financial_kpis_window",
        ),
        sa.CheckConstraint(
            "target_direction IS NULL OR target_direction IN ('lte', 'gte')",
            name="ck_financial_kpis_target_direction",
        ),
        sa.CheckConstraint(
            "(target_value IS NULL) = (target_direction IS NULL)",
            name="ck_financial_kpis_target_pair",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["currency_code"], ["currencies.code"]),
        sa.ForeignKeyConstraint(
            ["category_id", "workspace_id"],
            ["spending_categories.id", "spending_categories.workspace_id"],
            name="fk_financial_kpis_category_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["category_group_id", "workspace_id"],
            ["category_groups.id", "category_groups.workspace_id"],
            name="fk_financial_kpis_group_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "workspace_id"],
            ["accounts.id", "accounts.workspace_id"],
            name="fk_financial_kpis_account_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index("ix_financial_kpis_workspace_id", "financial_kpis", ["workspace_id"])
    op.create_index("ix_financial_kpis_category_id", "financial_kpis", ["category_id"])
    op.create_index("ix_financial_kpis_category_group_id", "financial_kpis", ["category_group_id"])
    op.create_index("ix_financial_kpis_account_id", "financial_kpis", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_financial_kpis_account_id", "financial_kpis")
    op.drop_index("ix_financial_kpis_category_group_id", "financial_kpis")
    op.drop_index("ix_financial_kpis_category_id", "financial_kpis")
    op.drop_index("ix_financial_kpis_workspace_id", "financial_kpis")
    op.drop_table("financial_kpis")
