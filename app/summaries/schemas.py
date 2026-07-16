from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from app.summaries.models import WeeklySummary


class WeeklySummaryResponse(BaseModel):
    public_id: uuid.UUID
    week_start: date
    week_end: date
    generated_at: datetime
    todo_summary: dict
    spending_summary: dict
    investing_summary: dict
    health_summary: dict | None = None
    dividend_summary: dict | None = None
    net_worth_summary: dict | None = None
    return_metrics_summary: dict | None = None
    highlights: dict
    read_at: datetime | None = None
    # spec-076 regeneration trail. is_superseded is derived from the ORM
    # object's internal superseded_by_id — never expose that raw internal id
    # over the API (BIGINT ids are internal-only; public_id is the contract).
    regenerated_at: datetime | None = None
    regeneration_reason: str | None = None
    is_superseded: bool = False

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_summary(cls, item: WeeklySummary) -> WeeklySummaryResponse:
        resp = cls.model_validate(item)
        resp.is_superseded = item.superseded_by_id is not None
        return resp


class RegenerateWeeklySummaryRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class WorkspaceSummarySettingResponse(BaseModel):
    cadence_day_of_week: int
    cadence_hour_utc: int
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkspaceSummarySettingUpdate(BaseModel):
    cadence_day_of_week: int = Field(ge=0, le=6)
    cadence_hour_utc: int = Field(ge=0, le=23)
