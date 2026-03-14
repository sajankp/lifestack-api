import uuid
from datetime import datetime

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
    workspace_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
