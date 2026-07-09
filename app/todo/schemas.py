import uuid
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.recurrence import MonthlyModeLiteral, OrdinalLiteral, validate_recurrence_fields
from app.todo.models import PriorityEnum


class TodoBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    description: str | None = Field(default="", max_length=500)
    due_date: datetime | None = Field(default=None)
    priority: PriorityEnum = Field(default=PriorityEnum.medium)
    completed: bool = Field(default=False)


class TodoCreate(TodoBase):
    parent_public_id: uuid.UUID | None = Field(default=None)


class TodoUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=500)
    due_date: datetime | None = Field(None)
    priority: PriorityEnum | None = Field(None)
    completed: bool | None = Field(None)
    parent_public_id: uuid.UUID | None = Field(None)


class TodoResponse(TodoBase):
    public_id: uuid.UUID
    parent_public_id: uuid.UUID | None = None
    subtask_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DeleteCompletedResponse(BaseModel):
    deleted: int


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
    monthly_mode: MonthlyModeLiteral = Field(default="day_of_month")
    by_weekday: int | None = Field(default=None, ge=0, le=6)
    by_ordinal: OrdinalLiteral | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_monthly_mode(self) -> "RecurringTodoRuleCreate":
        validate_recurrence_fields(
            self.frequency, self.monthly_mode, self.by_weekday, self.by_ordinal
        )
        return self


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
    monthly_mode: MonthlyModeLiteral | None = Field(default=None)
    by_weekday: int | None = Field(default=None, ge=0, le=6)
    by_ordinal: OrdinalLiteral | None = Field(default=None)


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
    monthly_mode: str
    by_weekday: int | None
    by_ordinal: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
