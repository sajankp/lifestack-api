"""Health measurements: a metric catalog, the records they came from, and the results (spec-110)."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

BODY_WEIGHT_CODE = "body_weight"


def _now() -> datetime:
    return datetime.now(UTC)


class HealthMetric(SQLModel, table=True):
    """Something that can be measured: ``body_weight``, ``hba1c``, ``ldl_cholesterol``..."""

    __tablename__ = "health_metrics"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)

    code: str = Field(max_length=64)
    name: str = Field(max_length=120)
    category: str = Field(default="other", max_length=32)
    value_type: str = Field(default="numeric", max_length=16)
    canonical_unit: str | None = Field(default=None, max_length=32)
    # {"<unit>": {"factor": "n", "offset": "n"}}: canonical = value * factor + offset.
    unit_conversions: dict | None = Field(default=None, sa_type=sa.JSON())
    aliases: list[str] | None = Field(default=None, sa_type=sa.JSON())
    min_value: Decimal | None = Field(default=None, sa_type=sa.Numeric(precision=18, scale=6))
    max_value: Decimal | None = Field(default=None, sa_type=sa.Numeric(precision=18, scale=6))

    created_at: datetime = Field(default_factory=_now, sa_type=sa.DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=_now, sa_type=sa.DateTime(timezone=True))

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "code", name="uq_health_metrics_workspace_code"),
        sa.CheckConstraint("value_type IN ('numeric', 'text')", name="ck_health_metrics_type"),
    )


class HealthRecord(SQLModel, table=True):
    """The document or event a set of results came from (a lab report, a prescription...)."""

    __tablename__ = "health_records"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    kind: str = Field(default="other", max_length=32)
    title: str = Field(sa_type=sa.Text())
    recorded_on: date = Field(sa_type=sa.Date())
    provider: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, sa_type=sa.Text())

    source_type: str = Field(default="mcp", max_length=32, index=True)
    source_ref: str | None = Field(default=None, max_length=255)
    source_import_id: int | None = Field(default=None, foreign_key="import_batches.id")

    created_at: datetime = Field(default_factory=_now, sa_type=sa.DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=_now, sa_type=sa.DateTime(timezone=True))

    __table_args__ = (
        sa.Index("ix_health_records_workspace_recorded_on", "workspace_id", "recorded_on"),
        sa.Index(
            "uq_health_records_workspace_source_ref",
            "workspace_id",
            "source_ref",
            unique=True,
            postgresql_where=sa.text("source_ref IS NOT NULL"),
        ),
    )


class HealthMeasurement(SQLModel, table=True):
    """One result of one metric on one day."""

    __tablename__ = "health_measurements"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    metric_id: int = Field(
        sa_column=sa.Column(
            sa.Integer(),
            sa.ForeignKey("health_metrics.id", ondelete="RESTRICT", name="fk_health_meas_metric"),
            nullable=False,
        )
    )
    record_id: int | None = Field(
        default=None,
        sa_column=sa.Column(
            sa.Integer(),
            sa.ForeignKey("health_records.id", ondelete="CASCADE", name="fk_health_meas_record"),
            nullable=True,
            index=True,
        ),
    )

    # The calendar day (user timezone). ``measured_at`` is set only when a real time of day
    # exists (weight, blood pressure): never a faked midnight.
    measured_on: date = Field(sa_type=sa.Date())
    measured_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))

    value_num: Decimal | None = Field(default=None, sa_type=sa.Numeric(precision=18, scale=6))
    comparator: str | None = Field(default=None, max_length=2)
    value_text: str | None = Field(default=None, sa_type=sa.Text())
    unit: str | None = Field(default=None, max_length=32)

    # The report's own reference range, in this row's ``unit``.
    ref_low: Decimal | None = Field(default=None, sa_type=sa.Numeric(precision=18, scale=6))
    ref_high: Decimal | None = Field(default=None, sa_type=sa.Numeric(precision=18, scale=6))
    ref_text: str | None = Field(default=None, sa_type=sa.Text())

    note: str | None = Field(default=None, sa_type=sa.Text())

    source_type: str = Field(default="manual", max_length=32, index=True)
    source_ref: str | None = Field(default=None, max_length=255)
    source_import_id: int | None = Field(default=None, foreign_key="import_batches.id")

    created_at: datetime = Field(default_factory=_now, sa_type=sa.DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=_now, sa_type=sa.DateTime(timezone=True))

    __table_args__ = (
        sa.CheckConstraint(
            "value_num IS NOT NULL OR value_text IS NOT NULL", name="ck_health_meas_has_value"
        ),
        sa.CheckConstraint(
            "comparator IS NULL OR value_num IS NOT NULL", name="ck_health_meas_comparator_numeric"
        ),
        sa.CheckConstraint(
            "comparator IS NULL OR comparator IN ('<', '<=', '>', '>=')",
            name="ck_health_meas_comparator",
        ),
        sa.Index("ix_health_meas_workspace_metric_day", "workspace_id", "metric_id", "measured_on"),
        sa.Index(
            "uq_health_meas_workspace_source_ref",
            "workspace_id",
            "source_ref",
            unique=True,
            postgresql_where=sa.text("source_ref IS NOT NULL"),
        ),
    )
