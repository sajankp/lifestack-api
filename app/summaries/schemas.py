from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from app.summaries.models import MonthlySummary, WeeklySummary


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
    # spec-086 Layers 2-3: read-time-only signal, never stored -- whether a
    # since-reverted import's live window overlaps this summary's net-worth/
    # investing boundary snapshot dates (the snapshot itself can never be
    # corrected, so this is an honest annotation, not a stale-data refresh
    # hint like data_stale would be).
    data_revised_after_snapshot: bool = False
    # spec-085: read-time-only signal, never stored -- whether a fresher
    # net-worth/investing boundary snapshot now exists than what this summary
    # was generated from.
    data_stale: bool = False

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_summary(
        cls,
        item: WeeklySummary,
        *,
        data_revised_after_snapshot: bool = False,
        data_stale: bool = False,
    ) -> WeeklySummaryResponse:
        resp = cls.model_validate(item)
        resp.is_superseded = item.superseded_by_id is not None
        resp.data_revised_after_snapshot = data_revised_after_snapshot
        resp.data_stale = data_stale
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


class MonthlySummaryResponse(BaseModel):
    public_id: uuid.UUID
    month_start: date
    month_end: date
    generated_at: datetime
    todo_summary: dict
    spending_summary: dict
    investing_summary: dict
    health_summary: dict | None = None
    dividend_summary: dict | None = None
    net_worth_summary: dict | None = None
    return_metrics_summary: dict | None = None
    behavioral_correlations: list[dict] | None = None
    highlights: dict
    read_at: datetime | None = None
    regenerated_at: datetime | None = None
    regeneration_reason: str | None = None
    is_superseded: bool = False
    data_revised_after_snapshot: bool = False
    data_stale: bool = False

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_summary(
        cls,
        item: MonthlySummary,
        *,
        data_revised_after_snapshot: bool = False,
        data_stale: bool = False,
    ) -> MonthlySummaryResponse:
        resp = cls.model_validate(item)
        resp.is_superseded = item.superseded_by_id is not None
        resp.data_revised_after_snapshot = data_revised_after_snapshot
        resp.data_stale = data_stale
        return resp


class RegenerateMonthlySummaryRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class GenerateMonthlySummaryRequest(BaseModel):
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
