"""store per-user MCP authorizations for connection management."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0065_mcp_grants"
down_revision: str | None = "0064_repair_recurring_next_dates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_grants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("client_name", sa.String(length=255), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint("user_id", "client_id", name="uq_mcp_grants_user_client"),
    )
    op.create_index("ix_mcp_grants_public_id", "mcp_grants", ["public_id"], unique=True)
    op.create_index("ix_mcp_grants_user_id", "mcp_grants", ["user_id"])
    op.create_index("ix_mcp_grants_client_id", "mcp_grants", ["client_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_grants_client_id", table_name="mcp_grants")
    op.drop_index("ix_mcp_grants_user_id", table_name="mcp_grants")
    op.drop_index("ix_mcp_grants_public_id", table_name="mcp_grants")
    op.drop_table("mcp_grants")
