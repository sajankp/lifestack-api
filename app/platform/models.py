import uuid
from datetime import UTC, datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class WorkspaceRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class Workspace(SQLModel, table=True):
    __tablename__ = "workspaces"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    name: str = Field(max_length=100)
    description: str | None = Field(default=None, max_length=500)
    is_active: bool = Field(default=True)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_type=sa.DateTime(timezone=True),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_type=sa.DateTime(timezone=True),
    )


class WorkspaceMembership(SQLModel, table=True):
    __tablename__ = "workspace_memberships"

    id: int | None = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    role: WorkspaceRole = Field(default=WorkspaceRole.MEMBER, sa_type=sa.String())

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_type=sa.DateTime(timezone=True),
    )


# Note: For Enum support in SQLModel/SQLAlchemy with Postgres, it's safer to use sa_type=sa.String() or sa.Enum()
# Since I'm using SQLModel, I'll refine the Membership model to be safer with sa_type.
