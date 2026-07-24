import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class MedicationFrequency(StrEnum):
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"


class MedicationEventStatus(StrEnum):
    taken = "taken"
    skipped = "skipped"


class Medication(SQLModel, table=True):
    __tablename__ = "medications"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    name: str = Field(max_length=120)
    dose_text: str | None = Field(default=None, max_length=60)
    refill_note: str | None = Field(default=None, max_length=500)

    # Schedule (reuses the RecurringTodoRule vocabulary, spec-069 owner
    # decision 1). days_of_week is the one extension: a JSON array of ints
    # 0-6 (Mon-Sun), only meaningful when frequency="weekly".
    frequency: str = Field(default=MedicationFrequency.daily.value, max_length=16)
    interval: int = Field(default=1, ge=1)
    # spec-092: "fixed" = derived-from-anchor calendar grid (default, unchanged);
    # "interval_from_last_dose" = next dose is intake-day + interval (daily only).
    schedule_mode: str = Field(default="fixed", max_length=24)
    days_of_week: list[int] | None = Field(default=None, sa_type=sa.JSON())
    anchor_date: date = Field(sa_type=sa.Date())
    end_date: date | None = Field(default=None, sa_type=sa.Date())
    timezone: str = Field(default="UTC", max_length=64)
    times: list[str] = Field(sa_type=sa.JSON())

    is_active: bool = Field(default=True)
    reminders_enabled: bool = Field(default=True)

    # Idempotency marker for medication_reminder_job — the slot datetime
    # (ISO string) most recently reminded, so a re-run doesn't double-push.
    last_reminded_slot: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))

    source_type: str = Field(default="manual", max_length=32, index=True)
    source_ref: str | None = Field(default=None, max_length=255)
    source_import_id: int | None = Field(default=None, foreign_key="import_batches.id")

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.CheckConstraint(
            "frequency IN ('daily', 'weekly', 'monthly')", name="ck_medications_frequency"
        ),
        sa.CheckConstraint("interval >= 1", name="ck_medications_interval_positive"),
        sa.CheckConstraint(
            "schedule_mode IN ('fixed', 'interval_from_last_dose')",
            name="ck_medications_schedule_mode",
        ),
    )


class MedicationEvent(SQLModel, table=True):
    __tablename__ = "medication_events"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    medication_id: int = Field(
        sa_column=sa.Column(
            sa.Integer(),
            sa.ForeignKey(
                "medications.id", ondelete="CASCADE", name="fk_medication_events_medication"
            ),
            index=True,
            nullable=False,
        )
    )

    scheduled_for: datetime = Field(sa_type=sa.DateTime(timezone=True))
    status: str = Field(max_length=16)
    logged_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    # spec-092: the moment the dose was actually taken (nullable — set for
    # "taken", NULL for "skipped"). interval_from_last_dose re-anchors off this.
    taken_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    note: str | None = Field(default=None, max_length=200)

    source_type: str = Field(default="manual", max_length=32, index=True)
    source_ref: str | None = Field(default=None, max_length=255)
    source_import_id: int | None = Field(default=None, foreign_key="import_batches.id")

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (
        sa.UniqueConstraint("medication_id", "scheduled_for", name="uq_medication_events_slot"),
        sa.CheckConstraint("status IN ('taken', 'skipped')", name="ck_medication_events_status"),
    )


class WeightEntry(SQLModel, table=True):
    __tablename__ = "weight_entries"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    measured_at: datetime = Field(sa_type=sa.DateTime(timezone=True))
    weight_kg: Decimal = Field(sa_type=sa.Numeric(precision=6, scale=2))
    note: str | None = Field(default=None, max_length=200)

    source_type: str = Field(default="manual", max_length=32, index=True)
    source_ref: str | None = Field(default=None, max_length=255)
    source_import_id: int | None = Field(default=None, foreign_key="import_batches.id")

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )

    __table_args__ = (sa.CheckConstraint("weight_kg > 0", name="ck_weight_entries_positive"),)
