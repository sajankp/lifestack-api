import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class McpGrant(SQLModel, table=True):
    """A user's durable authorization of one MCP client."""

    __tablename__ = "mcp_grants"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "client_id", name="uq_mcp_grants_user_client"),
    )

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    client_id: str = Field(max_length=255, index=True)
    client_name: str = Field(max_length=255)
    scopes: list[str] = Field(sa_column=sa.Column(sa.JSON, nullable=False, default=list))
    revoked_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    last_used_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
