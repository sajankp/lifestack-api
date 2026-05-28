"""create import batch tracking tables

Revision ID: 0015_create_import_batches
Revises: 0014_create_recurring_todo_rules
Create Date: 2026-05-28 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_create_import_batches"
down_revision: str | None = "0014_recurring_todo_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("module", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_backend", sa.String(length=16), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=True),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("valid_rows", sa.Integer(), nullable=False),
        sa.Column("error_rows", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_import_batches_public_id"), "import_batches", ["public_id"], unique=True
    )
    op.create_index(
        op.f("ix_import_batches_workspace_id"), "import_batches", ["workspace_id"], unique=False
    )
    op.create_index(op.f("ix_import_batches_user_id"), "import_batches", ["user_id"], unique=False)
    op.create_index(op.f("ix_import_batches_module"), "import_batches", ["module"], unique=False)
    op.create_index(op.f("ix_import_batches_status"), "import_batches", ["status"], unique=False)

    op.create_table(
        "import_errors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("import_batch_id", sa.Integer(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("field_name", sa.String(length=100), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=False),
        sa.Column("message", sa.String(length=1000), nullable=False),
        sa.Column("raw_value", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(["import_batch_id"], ["import_batches.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_import_errors_import_batch_id"), "import_errors", ["import_batch_id"], unique=False
    )
    op.create_index(
        op.f("ix_import_errors_row_number"), "import_errors", ["row_number"], unique=False
    )

    op.create_table(
        "import_preview_rows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("import_batch_id", sa.Integer(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["import_batch_id"], ["import_batches.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_import_preview_rows_import_batch_id"),
        "import_preview_rows",
        ["import_batch_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_import_preview_rows_row_number"),
        "import_preview_rows",
        ["row_number"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_import_preview_rows_row_number"), table_name="import_preview_rows")
    op.drop_index(op.f("ix_import_preview_rows_import_batch_id"), table_name="import_preview_rows")
    op.drop_table("import_preview_rows")

    op.drop_index(op.f("ix_import_errors_row_number"), table_name="import_errors")
    op.drop_index(op.f("ix_import_errors_import_batch_id"), table_name="import_errors")
    op.drop_table("import_errors")

    op.drop_index(op.f("ix_import_batches_status"), table_name="import_batches")
    op.drop_index(op.f("ix_import_batches_module"), table_name="import_batches")
    op.drop_index(op.f("ix_import_batches_user_id"), table_name="import_batches")
    op.drop_index(op.f("ix_import_batches_workspace_id"), table_name="import_batches")
    op.drop_index(op.f("ix_import_batches_public_id"), table_name="import_batches")
    op.drop_table("import_batches")
