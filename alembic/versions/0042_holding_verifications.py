"""add investing_holding_verifications (Demat CAS holdings verification, spec-060)

A read-only depository-vs-Lifestack holdings comparison snapshot, written
once per Demat CAS import commit. Never referenced by any money-bearing
table — deleting a row (import rollback) is always safe. ``source`` is a
plain string (not a named enum) since only NSDL ships in spec-060; adding
CDSL later needs no migration.

Revision ID: 0042_holding_verifications
Revises: 0041_default_spending_account
Create Date: 2026-07-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0042_holding_verifications"
down_revision = "0041_default_spending_account"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "investing_holding_verifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("source_import_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("statement_date", sa.Date(), nullable=True),
        sa.Column("match_count", sa.Integer(), nullable=False),
        sa.Column("quantity_drift_count", sa.Integer(), nullable=False),
        sa.Column("missing_in_lifestack_count", sa.Integer(), nullable=False),
        sa.Column("missing_at_depository_count", sa.Integer(), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id", "workspace_id"],
            ["accounts.id", "accounts.workspace_id"],
            name="fk_investing_holding_verifications_account_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_investing_holding_verifications_workspace",
        ),
        sa.ForeignKeyConstraint(
            ["source_import_id"],
            ["import_batches.id"],
            name="fk_investing_holding_verifications_import_batch",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_investing_holding_verifications_public_id"),
    )
    op.create_index(
        "ix_investing_holding_verifications_public_id",
        "investing_holding_verifications",
        ["public_id"],
    )
    op.create_index(
        "ix_investing_holding_verifications_workspace_id",
        "investing_holding_verifications",
        ["workspace_id"],
    )
    op.create_index(
        "ix_investing_holding_verifications_account_id",
        "investing_holding_verifications",
        ["account_id"],
    )
    op.create_index(
        "ix_investing_holding_verifications_source_import_id",
        "investing_holding_verifications",
        ["source_import_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_investing_holding_verifications_source_import_id",
        "investing_holding_verifications",
    )
    op.drop_index(
        "ix_investing_holding_verifications_account_id", "investing_holding_verifications"
    )
    op.drop_index(
        "ix_investing_holding_verifications_workspace_id", "investing_holding_verifications"
    )
    op.drop_index("ix_investing_holding_verifications_public_id", "investing_holding_verifications")
    op.drop_table("investing_holding_verifications")
