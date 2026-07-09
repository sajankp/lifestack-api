import uuid
from datetime import UTC, date, datetime, time
from enum import StrEnum

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

from app.core.recurrence import MonthlyRecurrenceMode


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

    # One level of subtasks (spec-068). NULL = top-level todo. Enforced
    # app-side that a parent cannot itself have a parent (see TodoService).
    parent_id: int | None = Field(
        default=None,
        sa_column=sa.Column(
            sa.Integer(),
            sa.ForeignKey("todos.id", ondelete="CASCADE", name="fk_todos_parent_id_todos"),
            index=True,
            nullable=True,
        ),
    )

    # Set when todo_reminder_job creates the due-reminder Notification
    # (spec-052) — makes the job idempotent without a joins-based "does a
    # notification already exist" probe. Reset to None on any due_date
    # change so a moved-later todo re-arms its reminder.
    reminded_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))

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
    due_time: time | None = Field(default=None, sa_type=sa.Time())
    timezone: str = Field(default="UTC", max_length=64)
    next_due_date: date = Field(sa_type=sa.Date())
    end_date: date | None = Field(default=None, sa_type=sa.Date())
    is_active: bool = Field(default=True)
    last_generated_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))

    # Calendar recurrence modes (spec-053) — only meaningful when
    # frequency="monthly"; enforced by a DB CHECK, not just app validation.
    monthly_mode: str = Field(
        default=MonthlyRecurrenceMode.day_of_month.value,
        sa_column=sa.Column(
            sa.Enum(
                "day_of_month",
                "last_day",
                "nth_weekday",
                name="recurrence_monthly_mode",
                create_type=False,
            ),
            nullable=False,
            server_default="day_of_month",
        ),
    )
    by_weekday: int | None = Field(default=None, sa_type=sa.SmallInteger())
    by_ordinal: int | None = Field(default=None, sa_type=sa.SmallInteger())

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.CheckConstraint(
            "(monthly_mode = 'nth_weekday') = (by_weekday IS NOT NULL AND by_ordinal IS NOT NULL)",
            name="ck_recurring_todo_rules_nth_weekday_fields",
        ),
        sa.CheckConstraint(
            "by_weekday IS NULL OR by_weekday BETWEEN 0 AND 6",
            name="ck_recurring_todo_rules_by_weekday_range",
        ),
        sa.CheckConstraint(
            "by_ordinal IS NULL OR by_ordinal IN (-1, 1, 2, 3, 4)",
            name="ck_recurring_todo_rules_by_ordinal_range",
        ),
    )
