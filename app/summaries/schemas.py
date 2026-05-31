import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class WeeklySummaryResponse(BaseModel):
    public_id: uuid.UUID
    week_start: date
    week_end: date
    generated_at: datetime
    todo_summary: dict
    spending_summary: dict
    investing_summary: dict
    highlights: dict

    model_config = ConfigDict(from_attributes=True)
