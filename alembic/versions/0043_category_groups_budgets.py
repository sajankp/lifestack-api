"""category_groups_budgets

Revision ID: 0043_category_groups_budgets
Revises: 0042_holding_verifications
Create Date: 2026-07-07 23:32:43.965791
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0043_category_groups_budgets"
down_revision = "0042_holding_verifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop existing spending_budgets table
    op.drop_table("spending_budgets")

    # 2. Create category_groups table
    op.create_table(
        "category_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("normalized_name", sa.String(length=100), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_category_groups_workspace"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "workspace_id", name="uq_category_group_id_workspace"),
        sa.UniqueConstraint(
            "workspace_id", "normalized_name", name="uq_category_group_workspace_name"
        ),
    )
    op.create_index(
        op.f("ix_category_groups_public_id"),
        "category_groups",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_category_groups_workspace_id"),
        "category_groups",
        ["workspace_id"],
        unique=False,
    )

    # 3. Add category_group_id column and foreign key to spending_categories
    op.add_column(
        "spending_categories", sa.Column("category_group_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        op.f("ix_spending_categories_category_group_id"),
        "spending_categories",
        ["category_group_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_spending_categories_group_workspace",
        "spending_categories",
        "category_groups",
        ["category_group_id", "workspace_id"],
        ["id", "workspace_id"],
    )

    # 4. Create the new spending_budgets table
    op.create_table(
        "spending_budgets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("category_group_id", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("start_month", sa.Date(), nullable=False),
        sa.Column("end_month", sa.Date(), nullable=True),
        sa.Column("source_type", sa.String(length=32), server_default="manual", nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("source_import_id", sa.Integer(), nullable=True),
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
            "(category_id IS NULL) != (category_group_id IS NULL)",
            name="ck_budget_scope",
        ),
        sa.ForeignKeyConstraint(
            ["category_id", "workspace_id"],
            ["spending_categories.id", "spending_categories.workspace_id"],
            name="fk_spending_budgets_category_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["category_group_id", "workspace_id"],
            ["category_groups.id", "category_groups.workspace_id"],
            name="fk_spending_budgets_group_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_import_id"],
            ["import_batches.id"],
            name="fk_spending_budgets_source_import_id",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_spending_budgets_workspace"
        ),
        sa.PrimaryKeyConstraint("id"),
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
    op.create_index(
        op.f("ix_spending_budgets_category_id"),
        "spending_budgets",
        ["category_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_spending_budgets_category_group_id"),
        "spending_budgets",
        ["category_group_id"],
        unique=False,
    )


def downgrade() -> None:
    # 1. Drop spending_budgets indices and table
    op.drop_index(op.f("ix_spending_budgets_category_group_id"), table_name="spending_budgets")
    op.drop_index(op.f("ix_spending_budgets_category_id"), table_name="spending_budgets")
    op.drop_index(op.f("ix_spending_budgets_workspace_id"), table_name="spending_budgets")
    op.drop_index(op.f("ix_spending_budgets_public_id"), table_name="spending_budgets")
    op.drop_table("spending_budgets")

    # 2. Drop constraint, index, and column from spending_categories
    op.drop_constraint(
        "fk_spending_categories_group_workspace", "spending_categories", type_="foreignkey"
    )
    op.drop_index(
        op.f("ix_spending_categories_category_group_id"), table_name="spending_categories"
    )
    op.drop_column("spending_categories", "category_group_id")

    # 3. Drop category_groups indices and table
    op.drop_index(op.f("ix_category_groups_workspace_id"), table_name="category_groups")
    op.drop_index(op.f("ix_category_groups_public_id"), table_name="category_groups")
    op.drop_table("category_groups")

    # 4. Recreate original spending_budgets table
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
