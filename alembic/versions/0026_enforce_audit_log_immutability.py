"""enforce_audit_log_immutability

Revision ID: 48f0b6946826
Revises: 82cc8f7000e6
Create Date: 2026-06-05 16:12:15.192597
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "48f0b6946826"
down_revision = "82cc8f7000e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION block_audit_log_modification()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'Audit logs are immutable and cannot be updated or deleted';
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_block_audit_log_modification
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW
        EXECUTE FUNCTION block_audit_log_modification();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_block_audit_log_modification ON audit_logs;")
    op.execute("DROP FUNCTION IF EXISTS block_audit_log_modification();")
