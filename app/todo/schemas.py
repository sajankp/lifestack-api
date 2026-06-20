import uuid
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.todo.models import PriorityEnum


class TodoBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(default="", max_length=500)
    due_date: datetime | None = Field(default=None)
    priority: PriorityEnum = Field(default=PriorityEnum.medium)
    completed: bool = Field(default=False)


class TodoCreate(TodoBase):
    pass


class TodoUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=500)
    due_date: datetime | None = Field(None)
    priority: PriorityEnum | None = Field(None)
    completed: bool | None = Field(None)


class TodoResponse(TodoBase):
    public_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RecurringTodoRuleCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(default="", max_length=500)
    priority: PriorityEnum = Field(default=PriorityEnum.medium)
    frequency: Literal["daily", "weekly", "monthly", "yearly"] = Field(default="weekly")
    interval: int = Field(default=1, ge=1)
    anchor_date: date
    due_time: time | None = None
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    end_date: date | None = None


class RecurringTodoRuleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    priority: PriorityEnum | None = Field(default=None)
    frequency: Literal["daily", "weekly", "monthly", "yearly"] | None = Field(default=None)
    interval: int | None = Field(default=None, ge=1)
    due_time: time | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    end_date: date | None = None
    is_active: bool | None = None


class RecurringTodoRuleResponse(BaseModel):
    public_id: uuid.UUID
    title: str
    description: str | None
    priority: PriorityEnum
    frequency: str
    interval: int
    anchor_date: date
    due_time: time | None
    timezone: str
    next_due_date: date
    end_date: date | None
    is_active: bool
    last_generated_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
