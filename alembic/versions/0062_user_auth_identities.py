"""Add multiple linkable authentication identities per user."""

import sqlalchemy as sa

from alembic import op

revision = "0062_user_auth_identities"
down_revision = "0061_add_oauth_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_auth_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("subject", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "subject", name="uq_user_auth_identities_provider_subject"),
        sa.UniqueConstraint("user_id", "provider", name="uq_user_auth_identities_user_provider"),
    )
    op.create_index("ix_user_auth_identities_user_id", "user_auth_identities", ["user_id"])
    op.create_index("ix_user_auth_identities_provider", "user_auth_identities", ["provider"])
    op.create_index("ix_user_auth_identities_subject", "user_auth_identities", ["subject"])
    op.execute(
        sa.text(
            "INSERT INTO user_auth_identities (user_id, provider, subject, created_at) "
            "SELECT id, oauth_provider, oauth_sub, CURRENT_TIMESTAMP FROM users "
            "WHERE oauth_provider IS NOT NULL AND oauth_sub IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_user_auth_identities_subject", table_name="user_auth_identities")
    op.drop_index("ix_user_auth_identities_provider", table_name="user_auth_identities")
    op.drop_index("ix_user_auth_identities_user_id", table_name="user_auth_identities")
    op.drop_table("user_auth_identities")
