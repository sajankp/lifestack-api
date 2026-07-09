import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

_TIME_RE_MSG = "times entries must be 'HH:MM' 24-hour strings"


def _validate_time_str(value: str) -> str:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(_TIME_RE_MSG)
    hour_str, minute_str = parts
    if not (
        hour_str.isdigit()
        and minute_str.isdigit()
        and len(hour_str) in (1, 2)
        and len(minute_str) == 2
    ):
        raise ValueError(_TIME_RE_MSG)
    hour, minute = int(hour_str), int(minute_str)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(_TIME_RE_MSG)
    return f"{hour:02d}:{minute:02d}"


class MedicationBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    dose_text: str | None = Field(default=None, max_length=60)
    refill_note: str | None = Field(default=None, max_length=500)
    frequency: Literal["daily", "weekly", "monthly"] = Field(default="daily")
    interval: int = Field(default=1, ge=1)
    days_of_week: list[int] | None = Field(default=None)
    anchor_date: date
    end_date: date | None = Field(default=None)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    times: list[str] = Field(..., min_length=1)
    is_active: bool = Field(default=True)
    reminders_enabled: bool = Field(default=True)

    @model_validator(mode="after")
    def _validate_schedule(self) -> "MedicationBase":
        if self.days_of_week is not None and self.frequency != "weekly":
            raise ValueError("days_of_week is only valid when frequency='weekly'")
        if self.frequency == "weekly":
            if not self.days_of_week:
                raise ValueError("days_of_week is required when frequency='weekly'")
            if any(d < 0 or d > 6 for d in self.days_of_week):
                raise ValueError("days_of_week entries must be 0-6 (Mon-Sun)")
        if self.end_date is not None and self.anchor_date > self.end_date:
            raise ValueError("anchor_date cannot be after end_date")
        self.times = sorted({_validate_time_str(t) for t in self.times})
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown timezone: {self.timezone}") from exc
        return self


class MedicationCreate(MedicationBase):
    pass


class MedicationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    dose_text: str | None = Field(default=None, max_length=60)
    refill_note: str | None = Field(default=None, max_length=500)
    frequency: Literal["daily", "weekly", "monthly"] | None = Field(default=None)
    interval: int | None = Field(default=None, ge=1)
    days_of_week: list[int] | None = Field(default=None)
    anchor_date: date | None = Field(default=None)
    end_date: date | None = Field(default=None)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    times: list[str] | None = Field(default=None, min_length=1)
    is_active: bool | None = Field(default=None)
    reminders_enabled: bool | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_update(self) -> "MedicationUpdate":
        if self.times is not None:
            self.times = sorted({_validate_time_str(t) for t in self.times})
        if self.timezone is not None:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"Unknown timezone: {self.timezone}") from exc
        if self.days_of_week is not None and any(d < 0 or d > 6 for d in self.days_of_week):
            raise ValueError("days_of_week entries must be 0-6 (Mon-Sun)")
        if (
            self.anchor_date is not None
            and self.end_date is not None
            and self.anchor_date > self.end_date
        ):
            raise ValueError("anchor_date cannot be after end_date")
        return self


class MedicationResponse(BaseModel):
    public_id: uuid.UUID
    name: str
    dose_text: str | None
    refill_note: str | None
    frequency: str
    interval: int
    days_of_week: list[int] | None
    anchor_date: date
    end_date: date | None
    timezone: str
    times: list[str]
    is_active: bool
    reminders_enabled: bool
    source_type: str
    event_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DoseSlot(BaseModel):
    medication_public_id: uuid.UUID
    medication_name: str
    dose_text: str | None
    scheduled_for: datetime
    status: Literal["pending", "taken", "skipped", "missed"]
    event_public_id: uuid.UUID | None = None
    note: str | None = None


class MedicationEventUpsert(BaseModel):
    scheduled_for: datetime
    status: Literal["taken", "skipped"]
    note: str | None = Field(default=None, max_length=200)


class MedicationEventResponse(BaseModel):
    public_id: uuid.UUID
    medication_public_id: uuid.UUID
    scheduled_for: datetime
    status: str
    logged_at: datetime
    note: str | None
    source_type: str

    model_config = ConfigDict(from_attributes=True)


class WeightEntryCreate(BaseModel):
    measured_at: datetime
    weight_kg: Decimal = Field(..., gt=0, decimal_places=2)
    note: str | None = Field(default=None, max_length=200)


class WeightEntryResponse(BaseModel):
    public_id: uuid.UUID
    measured_at: datetime
    weight_kg: Decimal
    note: str | None
    source_type: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WeightTrendResponse(BaseModel):
    entries: list[WeightEntryResponse]
    latest_kg: Decimal | None
    delta_7d_kg: Decimal | None
    delta_30d_kg: Decimal | None
    min_kg: Decimal | None
    max_kg: Decimal | None
