"""create spending tables

Revision ID: 0004
Revises: 0003_create_workspaces
Create Date: 2026-03-14 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_create_spending"
down_revision: str | None = "0003_create_workspaces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. spending_categories
    op.create_table(
        "spending_categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("normalized_name", sa.String(length=100), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("color", sa.String(length=20), nullable=True),
        sa.Column("icon", sa.String(length=50), nullable=True),
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
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_category_id_workspace"),
        sa.UniqueConstraint("workspace_id", "normalized_name", name="uq_category_workspace_name"),
    )
    op.create_index(
        op.f("ix_spending_categories_public_id"),
        "spending_categories",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_spending_categories_workspace_id"),
        "spending_categories",
        ["workspace_id"],
        unique=False,
    )

    # 2. spending_transactions
    op.create_table(
        "spending_transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(
            ["category_id", "workspace_id"],
            ["spending_categories.id", "spending_categories.workspace_id"],
            name="fk_spending_transactions_category_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_spending_transactions_public_id"),
        "spending_transactions",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_spending_transactions_workspace_id"),
        "spending_transactions",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_spending_transactions_category_id"),
        "spending_transactions",
        ["category_id"],
        unique=False,
    )

    # 3. spending_budgets
    op.create_table(
        "spending_budgets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("month_start", sa.Date(), nullable=False),
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
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(
            ["category_id", "workspace_id"],
            ["spending_categories.id", "spending_categories.workspace_id"],
            name="fk_spending_budgets_category_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "category_id",
            "month_start",
            name="uq_budget_workspace_category_month",
        ),
    )
    op.create_index(
        op.f("ix_spending_budgets_public_id"),
        "spending_budgets",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_spending_budgets_workspace_id"),
        "spending_budgets",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_spending_budgets_workspace_id"), table_name="spending_budgets")
    op.drop_index(op.f("ix_spending_budgets_public_id"), table_name="spending_budgets")
    op.drop_table("spending_budgets")

    op.drop_index(op.f("ix_spending_transactions_category_id"), table_name="spending_transactions")
    op.drop_index(op.f("ix_spending_transactions_workspace_id"), table_name="spending_transactions")
    op.drop_index(op.f("ix_spending_transactions_public_id"), table_name="spending_transactions")
    op.drop_table("spending_transactions")

    op.drop_index(op.f("ix_spending_categories_workspace_id"), table_name="spending_categories")
    op.drop_index(op.f("ix_spending_categories_public_id"), table_name="spending_categories")
    op.drop_table("spending_categories")
