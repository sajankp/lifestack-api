"""add user finance settings and workspace display preference

Revision ID: 0018_user_finance_settings
Revises: 0017_spending_tx_account
Create Date: 2026-05-31 16:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018_user_finance_settings"
down_revision: str | None = "0017_spending_tx_account"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspace_finance_settings",
        sa.Column(
            "currency_display_preference",
            sa.String(length=24),
            nullable=False,
            server_default="symbol",
        ),
    )
    op.alter_column(
        "workspace_finance_settings",
        "currency_display_preference",
        server_default=None,
    )

    op.create_table(
        "user_finance_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("reporting_currency_override_code", sa.String(length=10), nullable=True),
        sa.Column("currency_display_preference_override", sa.String(length=24), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["reporting_currency_override_code"], ["currencies.code"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            name="uq_user_finance_settings_workspace_user",
        ),
    )
    op.create_index(
        op.f("ix_user_finance_settings_workspace_id"),
        "user_finance_settings",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_finance_settings_user_id"),
        "user_finance_settings",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_finance_settings_user_id"), table_name="user_finance_settings")
    op.drop_index(
        op.f("ix_user_finance_settings_workspace_id"),
        table_name="user_finance_settings",
    )
    op.drop_table("user_finance_settings")

    op.drop_column("workspace_finance_settings", "currency_display_preference")
