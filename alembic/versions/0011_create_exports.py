"""create exports table

Revision ID: 0011_create_exports
Revises: 0010_investing_lookthrough
Create Date: 2026-05-25 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
import sqlmodel

from alembic import op

# revision identifiers, used by Alembic.
revision = "0011_create_exports"
down_revision = "0010_investing_lookthrough"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "exports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.Integer(), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("storage_key", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column("artifact_blob", sa.LargeBinary(), nullable=True),
        sa.Column(
            "artifact_mime_type", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=True
        ),
        sa.Column("artifact_filename", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column("error_message", sqlmodel.sql.sqltypes.AutoString(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index(op.f("ix_exports_public_id"), "exports", ["public_id"], unique=True)
    op.create_index(op.f("ix_exports_workspace_id"), "exports", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_exports_requested_by"), "exports", ["requested_by"], unique=False)
    op.create_index(
        "ix_exports_pending_workspace",
        "exports",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_exports_pending_workspace", table_name="exports")
    op.drop_index(op.f("ix_exports_requested_by"), table_name="exports")
    op.drop_index(op.f("ix_exports_workspace_id"), table_name="exports")
    op.drop_index(op.f("ix_exports_public_id"), table_name="exports")
    op.drop_table("exports")
