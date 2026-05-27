import uuid
from datetime import UTC, date, datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class PriorityEnum(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class Todo(SQLModel, table=True):
    __tablename__ = "todos"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    title: str = Field(max_length=100)
    description: str | None = Field(default="", max_length=500)
    due_date: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    priority: PriorityEnum = Field(default=PriorityEnum.medium, sa_type=sa.String())
    completed: bool = Field(default=False)

    system_key: str | None = Field(default=None, max_length=100, index=True)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "system_key", name="uq_todo_workspace_system_key"),
    )


class RecurringTodoRule(SQLModel, table=True):
    __tablename__ = "recurring_todo_rules"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    title: str = Field(max_length=100)
    description: str | None = Field(default="", max_length=500)
    priority: PriorityEnum = Field(default=PriorityEnum.medium, sa_type=sa.String())

    frequency: str = Field(default="weekly", max_length=16)
    interval: int = Field(default=1, ge=1)
    anchor_date: date = Field(sa_type=sa.Date())
    next_due_date: date = Field(sa_type=sa.Date())
    end_date: date | None = Field(default=None, sa_type=sa.Date())
    is_active: bool = Field(default=True)
    last_generated_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
