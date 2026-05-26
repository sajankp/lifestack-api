import uuid

from pydantic import BaseModel, Field


class CaptureHints(BaseModel):
    amount: str | None = None
    category: str | None = None
    due_date: str | None = None
    priority: str | None = None
    type: str | None = None


class CaptureRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    module: str | None = None
    hints: CaptureHints | None = None


class CaptureResponse(BaseModel):
    captured: bool
    module: str
    entity_public_id: uuid.UUID
    entity_type: str
    parsed: dict


class CaptureConflictResponse(BaseModel):
    captured: bool
    reason: str
    suggestions: list[str]
    message: str
