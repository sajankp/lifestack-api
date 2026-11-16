"""Request/response models for health measurements (spec-110)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_CODE_PATTERN = r"^[a-z][a-z0-9_]{1,63}$"
_SLUG_PATTERN = r"^[a-z0-9][a-z0-9_]{0,31}$"

# API-level limits for the TEXT columns (spec-110 "Column types"): they live only here.
TITLE_MAX = 160
RECORD_NOTE_MAX = 2000
MEASUREMENT_NOTE_MAX = 500
VALUE_TEXT_MAX = 500
REF_TEXT_MAX = 120
SOURCE_REF_MAX = 255
BATCH_MAX_ITEMS = 100


class MetricDefinition(BaseModel):
    """Create or update one metric in the catalog."""

    name: str = Field(..., min_length=1, max_length=120)
    code: str | None = Field(default=None, pattern=_CODE_PATTERN)
    category: str = Field(default="other", pattern=_SLUG_PATTERN)
    value_type: Literal["numeric", "text"] = "numeric"
    canonical_unit: str | None = Field(default=None, max_length=32)
    unit_conversions: dict | None = None
    aliases: list[str] | None = None
    min_value: Decimal | None = None
    max_value: Decimal | None = None

    @field_validator("aliases")
    @classmethod
    def _clean_aliases(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [a.strip() for a in value if a and a.strip()]
        if any(len(a) > 120 for a in cleaned):
            raise ValueError("aliases must be at most 120 characters each")
        return cleaned


class RecordHeader(BaseModel):
    """The record a bulk write attaches its results to."""

    kind: str = Field(default="other", pattern=_SLUG_PATTERN)
    title: str = Field(..., min_length=1, max_length=TITLE_MAX)
    recorded_on: date | None = None
    provider: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=RECORD_NOTE_MAX)
    source_ref: str | None = Field(default=None, max_length=SOURCE_REF_MAX)


class MetricLatest(BaseModel):
    measured_on: date
    value: Decimal | None
    value_text: str | None
    comparator: str | None
    unit: str | None


class MetricResponse(BaseModel):
    public_id: uuid.UUID
    code: str
    name: str
    category: str
    value_type: str
    canonical_unit: str | None
    unit_conversions: dict | None
    aliases: list[str]
    min_value: Decimal | None
    max_value: Decimal | None
    measurement_count: int = 0
    latest: MetricLatest | None = None

    model_config = ConfigDict(from_attributes=True)


class MeasurementPoint(BaseModel):
    public_id: uuid.UUID
    measured_on: date
    measured_at: datetime | None
    value: Decimal | None
    comparator: str | None
    value_text: str | None
    unit: str | None
    canonical_value: Decimal | None
    converted: bool = False
    ref_low: Decimal | None
    ref_high: Decimal | None
    ref_text: str | None
    in_range: Literal["low", "normal", "high"] | None
    record_public_id: uuid.UUID | None
    note: str | None
    source_type: str
    source_ref: str | None
    metric_code: str | None = None
    metric_name: str | None = None


class SeriesStats(BaseModel):
    count: int
    latest: Decimal | None = None
    latest_on: date | None = None
    min: Decimal | None = None
    max: Decimal | None = None
    delta_7d: Decimal | None = None
    delta_30d: Decimal | None = None


class SeriesResponse(BaseModel):
    metric: MetricResponse
    points: list[MeasurementPoint]
    stats: SeriesStats
    total: int
    limit: int
    offset: int
    has_more: bool


class RecordResponse(BaseModel):
    public_id: uuid.UUID
    kind: str
    title: str
    recorded_on: date
    provider: str | None
    note: str | None
    source_type: str
    source_ref: str | None
    measurement_count: int = 0
    created_at: datetime


class RecordDetailResponse(RecordResponse):
    measurements: list[MeasurementPoint]
