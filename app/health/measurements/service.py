"""Health measurements service: catalog, series, records, idempotent bulk writes (spec-110)."""

import difflib
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from app.core.audit import AuditLogger
from app.core.exceptions import NotFoundError
from app.health.measurements.models import (
    BODY_WEIGHT_CODE,
    HealthMeasurement,
    HealthMetric,
    HealthRecord,
)
from app.health.measurements.repository import (
    MeasurementRepository,
    MetricRepository,
    RecordRepository,
)
from app.health.measurements.schemas import (
    BATCH_MAX_ITEMS,
    MEASUREMENT_NOTE_MAX,
    REF_TEXT_MAX,
    SOURCE_REF_MAX,
    MeasurementPoint,
    MetricDefinition,
    MetricLatest,
    MetricResponse,
    RecordDetailResponse,
    RecordHeader,
    RecordResponse,
    SeriesResponse,
    SeriesStats,
)
from app.health.measurements.values import (
    UnitError,
    normalise_name,
    normalise_unit,
    parse_decimal,
    parse_value,
    range_flag,
    slugify_code,
    to_canonical,
    validate_conversions,
)

_SRC_MANUAL = "manual"
_SRC_MCP = "mcp"
_WEIGHT_UNIT = "kg"
_WEIGHT_MIN = Decimal("0.01")


class HealthMeasurementError(ValueError):
    """A validation failure that is safe to show to the caller verbatim."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _canonical_or_none(row: HealthMeasurement, metric: HealthMetric) -> Decimal | None:
    if row.value_num is None or metric.value_type != "numeric":
        return None
    try:
        return to_canonical(row.value_num, row.unit, metric.canonical_unit, metric.unit_conversions)
    except UnitError:
        return None


def compute_series_stats(
    window_rows: list[HealthMeasurement],
    metric: HealthMetric,
    today: date,
    history_rows: list[HealthMeasurement] | None = None,
) -> SeriesStats:
    """Latest / min / max / deltas in canonical units from newest-first rows.

    Threshold (``<0.5``) and text rows are counted but excluded from the arithmetic.
    ``history_rows`` (default: the window) is where a delta's earlier reading is looked up, so
    a window shorter than the delta span does not blank the delta.
    """
    stats = SeriesStats(count=len(window_rows))
    exact = _exact_canonical(window_rows, metric)
    if not exact:
        return stats
    latest_row, latest_value = exact[0]
    stats.latest = latest_value
    stats.latest_on = latest_row.measured_on
    values = [v for _, v in exact]
    stats.min, stats.max = min(values), max(values)
    history = exact if history_rows is None else _exact_canonical(history_rows, metric)

    def _delta(days_back: int) -> Decimal | None:
        cutoff = today - timedelta(days=days_back)
        if latest_row.measured_on <= cutoff:
            return None
        for row, value in history:
            if row.measured_on <= cutoff:
                return latest_value - value
        return None

    stats.delta_7d, stats.delta_30d = _delta(7), _delta(30)
    return stats


def _exact_canonical(
    rows: list[HealthMeasurement], metric: HealthMetric
) -> list[tuple[HealthMeasurement, Decimal]]:
    out: list[tuple[HealthMeasurement, Decimal]] = []
    for row in rows:
        if row.comparator is not None:
            continue
        value = _canonical_or_none(row, metric)
        if value is not None:
            out.append((row, value))
    return out


def to_point(
    row: HealthMeasurement,
    metric: HealthMetric,
    record_public_ids: dict[int, uuid.UUID],
    *,
    include_metric: bool = False,
) -> MeasurementPoint:
    canonical = _canonical_or_none(row, metric)
    converted = bool(
        canonical is not None
        and metric.canonical_unit is not None
        and normalise_unit(row.unit) != normalise_unit(metric.canonical_unit)
    )
    return MeasurementPoint(
        public_id=row.public_id,
        measured_on=row.measured_on,
        measured_at=row.measured_at,
        value=row.value_num,
        comparator=row.comparator,
        value_text=row.value_text,
        unit=row.unit,
        canonical_value=canonical,
        converted=converted,
        ref_low=row.ref_low,
        ref_high=row.ref_high,
        ref_text=row.ref_text,
        in_range=range_flag(row.value_num, row.comparator, row.ref_low, row.ref_high),
        record_public_id=record_public_ids.get(row.record_id) if row.record_id else None,
        note=row.note,
        source_type=row.source_type,
        source_ref=row.source_ref,
        metric_code=metric.code if include_metric else None,
        metric_name=metric.name if include_metric else None,
    )


def _metric_keys(metric: HealthMetric | Any) -> set[str]:
    keys = {normalise_name(metric.code), normalise_name(metric.name)}
    keys.update(normalise_name(a) for a in (metric.aliases or []))
    keys.discard("")
    return keys


def _measurement_snapshot(row: HealthMeasurement, metric_code: str) -> dict[str, Any]:
    """Audit snapshot: what is needed to undo, never note text (spec-110 privacy)."""
    return {
        "metric": metric_code,
        "measured_on": row.measured_on,
        "measured_at": row.measured_at,
        "value": row.value_num if row.value_num is not None else row.value_text,
        "comparator": row.comparator,
        "unit": row.unit,
    }


@dataclass
class _MetricView:
    """The slice of a metric that item validation needs (real or about-to-be-created)."""

    code: str
    name: str
    category: str
    value_type: str
    canonical_unit: str | None
    unit_conversions: dict | None
    min_value: Decimal | None
    max_value: Decimal | None
    real: HealthMetric | None = None


@dataclass
class _Plan:
    index: int
    status: str = "pending"
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metric_label: str | None = None
    metric: _MetricView | None = None
    new_metric: bool = False
    suggestions: list[str] = field(default_factory=list)
    fields: dict[str, Any] = field(default_factory=dict)
    key: tuple | None = None
    existing_id: uuid.UUID | None = None
    match_reason: str | None = None
    public_id: uuid.UUID | None = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MeasurementService:
    def __init__(
        self,
        metric_repo: MetricRepository,
        record_repo: RecordRepository,
        measurement_repo: MeasurementRepository,
    ) -> None:
        self.metrics = metric_repo
        self.records = record_repo
        self.measurements = measurement_repo

    @classmethod
    def from_session(cls, session: Any) -> "MeasurementService":
        return cls(
            MetricRepository(session), RecordRepository(session), MeasurementRepository(session)
        )

    # -- catalog ----------------------------------------------------------

    async def ensure_builtin_metrics(self, workspace_id: int) -> HealthMetric:
        """Create the workspace's ``body_weight`` metric if missing (race-safe, idempotent)."""
        existing = await self.metrics.get_by_code(workspace_id, BODY_WEIGHT_CODE)
        if existing is not None:
            return existing
        created = await self.metrics.insert_ignore_conflict(
            workspace_id,
            {
                "public_id": uuid.uuid4(),
                "code": BODY_WEIGHT_CODE,
                "name": "Body weight",
                "category": "body",
                "value_type": "numeric",
                "canonical_unit": _WEIGHT_UNIT,
                "unit_conversions": {},
                "aliases": ["weight"],
                "min_value": _WEIGHT_MIN,
            },
        )
        if created is not None:
            return created
        winner = await self.metrics.get_by_code(workspace_id, BODY_WEIGHT_CODE)
        if winner is None:  # pragma: no cover - the conflicting row cannot vanish mid-transaction
            raise HealthMeasurementError("Could not create the body_weight metric.")
        return winner

    async def _catalog(self, workspace_id: int, *, ensure: bool = False) -> list[HealthMetric]:
        """The workspace's metrics. Reads never write; only write paths pass ``ensure=True``."""
        if ensure:
            await self.ensure_builtin_metrics(workspace_id)
        return list(await self.metrics.list_for_workspace(workspace_id))

    @staticmethod
    def _index(catalog: list[HealthMetric]) -> dict[str, HealthMetric]:
        index: dict[str, HealthMetric] = {}
        for metric in catalog:
            for key in _metric_keys(metric):
                index.setdefault(key, metric)
        return index

    async def get_metric(self, workspace_id: int, ref: str) -> HealthMetric:
        """Resolve a metric by code, name or alias. Raises NotFoundError."""
        catalog = await self._catalog(workspace_id)
        metric = self._index(catalog).get(normalise_name(ref))
        if metric is None:
            raise NotFoundError(detail=f"Health metric '{ref}' not found")
        return metric

    def to_metric_response(
        self, metric: HealthMetric, count: int = 0, latest: HealthMeasurement | None = None
    ) -> MetricResponse:
        latest_view = (
            MetricLatest(
                measured_on=latest.measured_on,
                value=latest.value_num,
                value_text=latest.value_text,
                comparator=latest.comparator,
                unit=latest.unit,
            )
            if latest is not None
            else None
        )
        return MetricResponse(
            public_id=metric.public_id,
            code=metric.code,
            name=metric.name,
            category=metric.category,
            value_type=metric.value_type,
            canonical_unit=metric.canonical_unit,
            unit_conversions=metric.unit_conversions or None,
            aliases=list(metric.aliases or []),
            min_value=metric.min_value,
            max_value=metric.max_value,
            measurement_count=count,
            latest=latest_view,
        )

    async def list_metrics(self, workspace_id: int) -> list[MetricResponse]:
        catalog = await self._catalog(workspace_id)
        counts = await self.metrics.measurement_counts(workspace_id)
        latest = await self.measurements.latest_per_metric(workspace_id)
        return [self.to_metric_response(m, counts.get(m.id, 0), latest.get(m.id)) for m in catalog]

    async def define_metric(
        self,
        *,
        user_id: int,
        workspace_id: int,
        definition: MetricDefinition,
        audit_logger: AuditLogger | None = None,
    ) -> tuple[HealthMetric, str]:
        """Create or update a metric. Returns ``(metric, "created" | "updated")``."""
        try:
            code = definition.code or slugify_code(definition.name)
            conversions = validate_conversions(definition.unit_conversions)
        except ValueError as exc:
            raise HealthMeasurementError(str(exc)) from exc
        if definition.value_type == "text" and (definition.canonical_unit or conversions):
            raise HealthMeasurementError("A text metric cannot have units or conversions.")
        if conversions and not definition.canonical_unit:
            raise HealthMeasurementError("unit_conversions need a canonical_unit.")
        if definition.canonical_unit and normalise_unit(definition.canonical_unit) in {
            normalise_unit(u) for u in conversions
        }:
            raise HealthMeasurementError(
                "A conversion cannot be registered for the canonical unit."
            )
        if (
            definition.min_value is not None
            and definition.max_value is not None
            and definition.min_value > definition.max_value
        ):
            raise HealthMeasurementError("min_value must not exceed max_value.")

        catalog = await self._catalog(workspace_id, ensure=True)
        existing = next((m for m in catalog if m.code == code), None)
        new_keys = {normalise_name(code), normalise_name(definition.name)}
        new_keys.update(normalise_name(a) for a in (definition.aliases or []))
        new_keys.discard("")
        for other in catalog:
            if existing is not None and other.id == existing.id:
                continue
            clash = new_keys & _metric_keys(other)
            if clash:
                raise HealthMeasurementError(
                    f"'{sorted(clash)[0]}' already resolves to metric '{other.code}'."
                )

        values = {
            "name": definition.name.strip(),
            "category": definition.category,
            "value_type": definition.value_type,
            "canonical_unit": definition.canonical_unit.strip()
            if definition.canonical_unit
            else None,
            "unit_conversions": conversions,
            "aliases": list(definition.aliases or []),
            "min_value": definition.min_value,
            "max_value": definition.max_value,
        }
        if existing is None:
            created = await self.metrics.insert_ignore_conflict(
                workspace_id, {"public_id": uuid.uuid4(), "code": code, **values}
            )
            if created is None:
                raise HealthMeasurementError(f"Metric '{code}' already exists.")
            await self._audit_metric(audit_logger, user_id, workspace_id, "create", created)
            return created, "created"

        count = await self.measurements.count_for_metric(workspace_id, existing.id)
        if count:
            if values["value_type"] != existing.value_type:
                raise HealthMeasurementError(
                    f"Cannot change value_type of '{code}': it already has {count} measurement(s)."
                )
            if normalise_unit(values["canonical_unit"]) != normalise_unit(existing.canonical_unit):
                raise HealthMeasurementError(
                    f"Cannot change the canonical unit of '{code}': it has {count} measurement(s)."
                )
            used_units = await self.measurements.distinct_units(workspace_id, existing.id)
            allowed = {normalise_unit(values["canonical_unit"])} | {
                normalise_unit(u) for u in conversions
            }
            missing = sorted(u for u in used_units if normalise_unit(u) not in allowed and u)
            if missing:
                raise HealthMeasurementError(
                    f"Cannot drop unit(s) {missing} from '{code}': measurements use them."
                )
        for attr, value in values.items():
            setattr(existing, attr, value)
        existing.updated_at = datetime.now(UTC)
        existing = await self.metrics.save(existing)
        await self._audit_metric(audit_logger, user_id, workspace_id, "update", existing)
        return existing, "updated"

    @staticmethod
    async def _audit_metric(
        audit_logger: AuditLogger | None,
        user_id: int,
        workspace_id: int,
        action: str,
        metric: HealthMetric,
    ) -> None:
        if audit_logger is None:
            return
        snap = {"code": metric.code, "name": metric.name, "canonical_unit": metric.canonical_unit}
        await audit_logger.log(
            workspace_id=workspace_id,
            actor_id=user_id,
            action=action,
            module="health",
            entity_type="health_metric",
            entity_id=metric.id,
            details={
                "entity_public_id": str(metric.public_id),
                "before": None if action == "create" else snap,
                "after": snap,
                "changed_fields": list(snap.keys()),
            },
        )

    # -- reads ------------------------------------------------------------

    async def _today(self, user_id: int | None) -> date:
        tz: tzinfo = (
            await self.measurements.get_user_timezone(user_id) if user_id is not None else UTC
        )
        return datetime.now(tz).date()

    async def _record_ids(
        self, workspace_id: int, rows: list[HealthMeasurement]
    ) -> dict[int, uuid.UUID]:
        records = await self.records.get_by_ids(
            workspace_id, [r.record_id for r in rows if r.record_id is not None]
        )
        return {rid: rec.public_id for rid, rec in records.items()}

    async def get_series(
        self,
        workspace_id: int,
        ref: str,
        *,
        from_on: date | None = None,
        to_on: date | None = None,
        limit: int = 25,
        offset: int = 0,
        user_id: int | None = None,
    ) -> SeriesResponse:
        metric = await self.get_metric(workspace_id, ref)
        rows = await self.measurements.list_for_metric(
            workspace_id, metric.id, from_on=from_on, to_on=to_on
        )
        today = await self._today(user_id)
        page = rows[offset : offset + limit]
        record_ids = await self._record_ids(workspace_id, page)
        return SeriesResponse(
            metric=self.to_metric_response(metric, len(rows), rows[0] if rows else None),
            points=[to_point(r, metric, record_ids) for r in page],
            stats=compute_series_stats(rows, metric, today),
            total=len(rows),
            limit=limit,
            offset=offset,
            has_more=offset + len(page) < len(rows),
        )

    def _to_record_response(self, record: HealthRecord, count: int) -> RecordResponse:
        return RecordResponse(
            public_id=record.public_id,
            kind=record.kind,
            title=record.title,
            recorded_on=record.recorded_on,
            provider=record.provider,
            note=record.note,
            source_type=record.source_type,
            source_ref=record.source_ref,
            measurement_count=count,
            created_at=record.created_at,
        )

    async def list_records(
        self,
        workspace_id: int,
        *,
        kind: str | None = None,
        from_on: date | None = None,
        to_on: date | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[RecordResponse], int]:
        items, total = await self.records.list_for_workspace(
            workspace_id, kind, from_on, to_on, limit, offset
        )
        counts = await self.records.measurement_counts([r.id for r in items])
        return [self._to_record_response(r, counts.get(r.id, 0)) for r in items], total

    async def get_record(self, workspace_id: int, public_id: uuid.UUID) -> RecordDetailResponse:
        record = await self.records.get_by_public_id(workspace_id, public_id)
        if record is None:
            raise NotFoundError(detail=f"Health record with id {public_id} not found")
        rows = await self.measurements.list_for_record(record.id)
        catalog = {m.id: m for m in await self._catalog(workspace_id)}
        base = self._to_record_response(record, len(rows))
        return RecordDetailResponse(
            **base.model_dump(),
            measurements=[
                to_point(
                    r, catalog[r.metric_id], {record.id: record.public_id}, include_metric=True
                )
                for r in rows
                if r.metric_id in catalog
            ],
        )

    # -- deletes ----------------------------------------------------------

    async def delete_measurement(
        self,
        *,
        user_id: int,
        workspace_id: int,
        public_id: uuid.UUID,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        row = await self.measurements.get_by_public_id(workspace_id, public_id)
        if row is None:
            raise NotFoundError(detail=f"Health measurement with id {public_id} not found")
        code = next(
            (m.code for m in await self._catalog(workspace_id) if m.id == row.metric_id), "?"
        )
        before = _measurement_snapshot(row, code)
        await self.measurements.delete(row)
        await self._audit_measurement(
            audit_logger, user_id, workspace_id, "delete", row, before, None
        )

    async def delete_record(
        self,
        *,
        user_id: int,
        workspace_id: int,
        public_id: uuid.UUID,
        confirmed: bool,
        audit_logger: AuditLogger | None = None,
    ) -> dict[str, Any]:
        record = await self.records.get_by_public_id(workspace_id, public_id)
        if record is None:
            raise NotFoundError(detail=f"Health record with id {public_id} not found")
        count = (await self.records.measurement_counts([record.id])).get(record.id, 0)
        if not confirmed:
            return {
                "status": "preview",
                "deleted": False,
                "measurement_count": count,
                "message": (
                    f"Would delete record '{record.title}' and its {count} measurement(s). "
                    "Pass confirmed=true to delete."
                ),
            }
        snap = {"kind": record.kind, "title": record.title, "measurement_count": count}
        await self.records.delete(record)  # measurements go with it (ON DELETE CASCADE)
        if audit_logger is not None:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="delete",
                module="health",
                entity_type="health_record",
                entity_id=record.id,
                details={
                    "entity_public_id": str(record.public_id),
                    "before": snap,
                    "after": None,
                    "changed_fields": [],
                },
            )
        return {"status": "success", "deleted": True, "measurement_count": count}

    @staticmethod
    async def _audit_measurement(
        audit_logger: AuditLogger | None,
        user_id: int,
        workspace_id: int,
        action: str,
        row: HealthMeasurement,
        before: dict | None,
        after: dict | None,
    ) -> None:
        if audit_logger is None:
            return
        snap = after if after is not None else before or {}
        await audit_logger.log(
            workspace_id=workspace_id,
            actor_id=user_id,
            action=action,
            module="health",
            entity_type="health_measurement",
            entity_id=row.id,
            details={
                "entity_public_id": str(row.public_id),
                "before": before,
                "after": after,
                "changed_fields": list(snap.keys()) if action == "create" else [],
            },
        )

    # -- bulk write -------------------------------------------------------

    async def record_results(
        self,
        *,
        user_id: int,
        workspace_id: int,
        record: dict | None,
        items: list[dict],
        atomic: bool = False,
        dry_run: bool = True,
        skip_duplicates: bool = False,
        create_missing_metrics: bool = False,
        audit_logger: AuditLogger | None = None,
    ) -> dict[str, Any]:
        """Validate, dedupe and (unless ``dry_run``) persist a batch under one record."""
        if not isinstance(items, list) or not items:
            return _error("items must be a non-empty list.")
        if len(items) > BATCH_MAX_ITEMS:
            return _error(f"At most {BATCH_MAX_ITEMS} items per call (got {len(items)}).")

        tz = await self.measurements.get_user_timezone(user_id)
        today = datetime.now(tz).date()
        try:
            header = RecordHeader.model_validate(
                record if record else {"kind": "import", "title": f"Bulk write {today.isoformat()}"}
            )
        except PydanticValidationError as exc:
            return _error(f"Invalid record header: {_first_error(exc)}")
        recorded_on = header.recorded_on or today
        if recorded_on > today + timedelta(days=1):
            return _error("record.recorded_on must not be in the future.")

        if not dry_run:
            await self.records.lock_workspace_writes(workspace_id)
        existing_record = (
            await self.records.get_by_source_ref(workspace_id, header.source_ref)
            if header.source_ref
            else None
        )

        catalog = await self._catalog(workspace_id, ensure=True)
        index = self._index(catalog)
        pending: dict[str, _MetricView] = {}
        plans = [
            self._plan_item(
                i, raw, header, recorded_on, index, pending, create_missing_metrics, tz, today
            )
            for i, raw in enumerate(items)
        ]
        await self._dedupe(workspace_id, plans, existing_record, skip_duplicates=skip_duplicates)

        for plan in plans:
            if plan.status == "pending":
                plan.status = "would_create" if dry_run else "create"

        counts = _counts(plans)
        rejected = atomic and (counts["invalid"] or counts["unknown_metric"])
        if rejected:
            return {
                "status": "error",
                "dry_run": dry_run,
                "message": (
                    f"Atomic batch rejected: {counts['invalid']} invalid and "
                    f"{counts['unknown_metric']} unknown-metric item(s). Nothing was written."
                ),
                "counts": counts,
                "items": [_item_view(p) for p in plans],
            }
        if dry_run:
            return {
                "status": "success",
                "dry_run": True,
                "record": {
                    "public_id": str(existing_record.public_id) if existing_record else None,
                    "created": False,
                    "reused": existing_record is not None,
                },
                "counts": counts,
                "items": [_item_view(p) for p in plans],
            }

        return await self._persist(
            user_id=user_id,
            workspace_id=workspace_id,
            header=header,
            recorded_on=recorded_on,
            existing_record=existing_record,
            plans=plans,
            catalog=catalog,
            counts=counts,
            audit_logger=audit_logger,
        )

    async def _persist(
        self,
        *,
        user_id: int,
        workspace_id: int,
        header: RecordHeader,
        recorded_on: date,
        existing_record: HealthRecord | None,
        plans: list[_Plan],
        catalog: list[HealthMetric],
        counts: dict[str, int],
        audit_logger: AuditLogger | None,
    ) -> dict[str, Any]:
        to_create = [p for p in plans if p.status == "create"]
        record = existing_record
        record_created = False
        if to_create and record is None:
            record = await self.records.create(
                HealthRecord(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    kind=header.kind,
                    title=header.title,
                    recorded_on=recorded_on,
                    provider=header.provider,
                    note=header.note,
                    source_type=_SRC_MCP,
                    source_ref=header.source_ref,
                )
            )
            record_created = True

        by_code = {m.code: m for m in catalog}
        for plan in to_create:
            view = plan.metric
            assert view is not None
            metric = view.real or by_code.get(view.code)
            if metric is None:
                metric = await self.metrics.insert_ignore_conflict(
                    workspace_id,
                    {
                        "public_id": uuid.uuid4(),
                        "code": view.code,
                        "name": view.name,
                        "category": view.category,
                        "value_type": view.value_type,
                        "canonical_unit": view.canonical_unit,
                        "unit_conversions": {},
                        "aliases": [],
                    },
                ) or await self.metrics.get_by_code(workspace_id, view.code)
                by_code[view.code] = metric
            row = await self.measurements.create(
                HealthMeasurement(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    metric_id=metric.id,
                    record_id=record.id if record is not None else None,
                    source_type=_SRC_MCP,
                    **plan.fields,
                )
            )
            plan.public_id = row.public_id
            plan.status = "created"

        if audit_logger is not None and to_create and record is not None:
            created_ids = [str(p.public_id) for p in to_create]
            snap = {
                "kind": record.kind,
                "title": record.title,
                "measurement_count": len(to_create),
                "measurement_public_ids": created_ids,
            }
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="create" if record_created else "update",
                module="health",
                entity_type="health_record",
                entity_id=record.id,
                details={
                    "entity_public_id": str(record.public_id),
                    "before": None if record_created else {"measurement_count": 0},
                    "after": snap,
                    "changed_fields": list(snap.keys()),
                },
            )

        return {
            "status": "success",
            "dry_run": False,
            "record": {
                "public_id": str(record.public_id) if record is not None else None,
                "created": record_created,
                "reused": existing_record is not None,
            },
            "counts": counts,
            "items": [_item_view(p) for p in plans],
        }

    # -- bulk write: per-item validation ----------------------------------

    def _plan_item(
        self,
        index_no: int,
        raw: Any,
        header: RecordHeader,
        recorded_on: date,
        index: dict[str, HealthMetric],
        pending: dict[str, _MetricView],
        create_missing: bool,
        tz: tzinfo,
        today: date,
    ) -> _Plan:
        plan = _Plan(index=index_no)
        if not isinstance(raw, dict):
            plan.status, plan.errors = "invalid", ["item must be an object."]
            return plan
        label = raw.get("metric")
        if not isinstance(label, str) or not label.strip():
            plan.status, plan.errors = "invalid", ["metric is required."]
            return plan
        plan.metric_label = label.strip()
        key = normalise_name(label)

        real = index.get(key)
        view = pending.get(key)
        if real is not None:
            view = _MetricView(
                real.code,
                real.name,
                real.category,
                real.value_type,
                real.canonical_unit,
                real.unit_conversions,
                real.min_value,
                real.max_value,
                real=real,
            )
        elif view is None:
            if not create_missing:
                close = difflib.get_close_matches(key, list(index.keys()), n=3, cutoff=0.6)
                names: list[str] = []
                for hit in close:
                    if index[hit].name not in names:
                        names.append(index[hit].name)
                plan.status = "unknown_metric"
                plan.suggestions = names
                plan.errors = [
                    f"Unknown metric '{plan.metric_label}'. Define it with define_health_metric "
                    "or pass create_missing_metrics=true."
                ]
                return plan
            view = self._pending_view(plan.metric_label, raw, header)
            if view is None:
                plan.status, plan.errors = (
                    "invalid",
                    [f"Cannot create a metric from '{plan.metric_label}'."],
                )
                return plan
            pending[key] = view
            plan.new_metric = True
        plan.metric = view
        if view.real is None and not plan.new_metric:
            plan.new_metric = True  # a later item re-using a metric created earlier in the batch

        errors = plan.errors
        value_num, comparator, value_text = self._parse_item_value(raw, view, errors)
        unit = raw.get("unit")
        if unit is not None and not isinstance(unit, str):
            errors.append("unit must be a string.")
            unit = None
        unit = unit.strip() if isinstance(unit, str) and unit.strip() else None
        if view.value_type == "numeric" and value_num is not None:
            try:
                canonical = to_canonical(
                    value_num, unit, view.canonical_unit, view.unit_conversions
                )
            except UnitError as exc:
                errors.append(str(exc))
            else:
                self._check_bounds(canonical, view, errors)
        if view.value_type == "text":
            unit = None

        measured_on, measured_at = self._parse_dates(raw, recorded_on, tz, today, errors)
        ref_low, ref_high = self._parse_range(raw, view, errors)
        ref_text = self._opt_str(raw, "ref_text", REF_TEXT_MAX, errors)
        note = self._opt_str(raw, "note", MEASUREMENT_NOTE_MAX, errors)
        source_ref = self._opt_str(raw, "source_ref", SOURCE_REF_MAX, errors)

        if errors:
            plan.status = "invalid"
            return plan
        plan.fields = {
            "measured_on": measured_on,
            "measured_at": measured_at,
            "value_num": value_num,
            "comparator": comparator,
            "value_text": value_text,
            "unit": unit,
            "ref_low": ref_low,
            "ref_high": ref_high,
            "ref_text": ref_text,
            "note": note,
            "source_ref": source_ref,
        }
        plan.key = (
            view.code,
            measured_on,
            measured_at,
            value_num,
            comparator,
            value_text,
            normalise_unit(unit),
        )
        return plan

    @staticmethod
    def _pending_view(label: str, raw: dict, header: RecordHeader) -> _MetricView | None:
        try:
            code = slugify_code(label)
        except ValueError:
            return None
        value_type = "numeric"
        try:
            parse_value(str(raw.get("value", "")), "numeric")
        except ValueError:
            value_type = "text"
        unit = raw.get("unit")
        unit = unit.strip() if isinstance(unit, str) and unit.strip() else None
        category = raw.get("category")
        if not (isinstance(category, str) and category.strip()):
            category = header.kind
        return _MetricView(
            code=code,
            name=label.strip()[:120],
            category=category.strip().lower()[:32],
            value_type=value_type,
            canonical_unit=unit if value_type == "numeric" else None,
            unit_conversions=None,
            min_value=None,
            max_value=None,
        )

    @staticmethod
    def _parse_item_value(
        raw: dict, view: _MetricView, errors: list[str]
    ) -> tuple[Decimal | None, str | None, str | None]:
        value = raw.get("value")
        if isinstance(value, bool) or value is None:
            errors.append("value is required.")
            return None, None, None
        try:
            parsed = parse_value(str(value), view.value_type)
        except ValueError as exc:
            errors.append(str(exc))
            return None, None, None
        return parsed.value_num, parsed.comparator, parsed.value_text

    @staticmethod
    def _check_bounds(canonical: Decimal, view: _MetricView, errors: list[str]) -> None:
        if view.min_value is not None and canonical < view.min_value:
            errors.append(f"value is below the minimum for this metric ({view.min_value}).")
        if view.max_value is not None and canonical > view.max_value:
            errors.append(f"value is above the maximum for this metric ({view.max_value}).")

    @staticmethod
    def _parse_dates(
        raw: dict, recorded_on: date, tz: tzinfo, today: date, errors: list[str]
    ) -> tuple[date, datetime | None]:
        measured_at: datetime | None = None
        if raw.get("measured_at") not in (None, ""):
            try:
                parsed = datetime.fromisoformat(str(raw["measured_at"]).strip())
            except ValueError:
                errors.append("measured_at must be an ISO-8601 datetime.")
            else:
                measured_at = (
                    parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed
                ).astimezone(UTC)
        measured_on = recorded_on
        if raw.get("measured_on") not in (None, ""):
            try:
                measured_on = date.fromisoformat(str(raw["measured_on"]).strip())
            except ValueError:
                errors.append("measured_on must be a YYYY-MM-DD date.")
        elif measured_at is not None:
            measured_on = measured_at.astimezone(tz).date()
        if (
            measured_at is not None
            and raw.get("measured_on") not in (None, "")
            and measured_at.astimezone(tz).date() != measured_on
        ):
            errors.append("measured_at does not fall on measured_on in your timezone.")
        if measured_on > today + timedelta(days=1):
            errors.append("measured_on must not be in the future.")
        return measured_on, measured_at

    @staticmethod
    def _parse_range(
        raw: dict, view: _MetricView, errors: list[str]
    ) -> tuple[Decimal | None, Decimal | None]:
        bounds: dict[str, Decimal | None] = {"ref_low": None, "ref_high": None}
        for name in bounds:
            if raw.get(name) in (None, ""):
                continue
            if view.value_type == "text":
                errors.append(f"{name} is only valid for numeric metrics.")
                continue
            try:
                bounds[name] = parse_decimal(raw[name], name)
            except ValueError as exc:
                errors.append(str(exc))
        low, high = bounds["ref_low"], bounds["ref_high"]
        if low is not None and high is not None and low > high:
            errors.append("ref_low must not exceed ref_high.")
        return low, high

    @staticmethod
    def _opt_str(raw: dict, name: str, limit: int, errors: list[str]) -> str | None:
        value = raw.get(name)
        if value is None:
            return None
        if not isinstance(value, str):
            errors.append(f"{name} must be a string.")
            return None
        value = value.strip()
        if len(value) > limit:
            errors.append(f"{name} must be at most {limit} characters.")
            return None
        return value or None

    # -- bulk write: dedupe -----------------------------------------------

    async def _dedupe(
        self,
        workspace_id: int,
        plans: list[_Plan],
        existing_record: HealthRecord | None,
        *,
        skip_duplicates: bool,
    ) -> None:
        live = [p for p in plans if p.status == "pending"]
        metric_ids = [p.metric.real.id for p in live if p.metric and p.metric.real]
        days = [p.fields["measured_on"] for p in live]
        found = await self.measurements.find_for_dedupe(workspace_id, metric_ids, days)
        code_by_id = {m.id: m.code for m in await self.metrics.list_for_workspace(workspace_id)}
        by_key: dict[tuple, list[HealthMeasurement]] = defaultdict(list)
        for row in found:
            by_key[
                (
                    code_by_id.get(row.metric_id),
                    row.measured_on,
                    row.measured_at,
                    row.value_num,
                    row.comparator,
                    row.value_text,
                    normalise_unit(row.unit),
                )
            ].append(row)

        consumed: dict[tuple, int] = defaultdict(int)
        seen_refs: set[str] = set()
        for plan in live:
            ref = plan.fields.get("source_ref")
            if ref:
                if ref in seen_refs:
                    plan.status, plan.match_reason = "duplicate", "source_ref"
                    plan.warnings.append("Same source_ref appears earlier in this batch.")
                    continue
                seen_refs.add(ref)
                match = await self.measurements.get_by_source_ref(workspace_id, ref)
                if match is not None:
                    plan.status, plan.match_reason = "duplicate", "source_ref"
                    plan.existing_id = match.public_id
                    continue
            rows = by_key.get(plan.key, [])
            used = consumed[plan.key]
            if used < len(rows):
                matched = rows[used]
                consumed[plan.key] += 1
                plan.existing_id = matched.public_id
                same_record = (
                    existing_record is not None and matched.record_id == existing_record.id
                )
                if same_record:
                    plan.status, plan.match_reason = "duplicate", "record_source_ref"
                elif skip_duplicates:
                    plan.status, plan.match_reason = "duplicate", "natural_key"
                else:
                    plan.status, plan.match_reason = "possible_duplicate", "natural_key"
                    plan.warnings.append(
                        "A matching reading already exists; not written. "
                        "Pass skip_duplicates=true to skip silently."
                    )

    # -- weight (spec-110 §D: weight is the ``body_weight`` metric) --------

    async def log_weight(
        self,
        *,
        user_id: int,
        workspace_id: int,
        weight_kg: Decimal,
        measured_at: datetime,
        note: str | None,
        audit_logger: AuditLogger | None = None,
    ) -> HealthMeasurement:
        if weight_kg < _WEIGHT_MIN:
            raise HealthMeasurementError("Weight must be a positive number.")
        metric = await self.ensure_builtin_metrics(workspace_id)
        tz = await self.measurements.get_user_timezone(user_id)
        when = measured_at if measured_at.tzinfo else measured_at.replace(tzinfo=UTC)
        row = await self.measurements.create(
            HealthMeasurement(
                workspace_id=workspace_id,
                user_id=user_id,
                metric_id=metric.id,
                measured_on=when.astimezone(tz).date(),
                measured_at=when.astimezone(UTC),
                value_num=weight_kg,
                unit=_WEIGHT_UNIT,
                note=note,
                source_type=_SRC_MANUAL,
            )
        )
        after = _measurement_snapshot(row, metric.code)
        await self._audit_measurement(
            audit_logger, user_id, workspace_id, "create", row, None, after
        )
        return row

    async def list_weight(
        self,
        workspace_id: int,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[HealthMeasurement], int]:
        metric = await self._weight_metric(workspace_id)
        if metric is None:
            return [], 0
        rows = await self.measurements.list_for_metric(
            workspace_id, metric.id, start_at=start_at, end_at=end_at
        )
        return rows[offset : offset + limit], len(rows)

    async def _weight_metric(self, workspace_id: int) -> HealthMetric | None:
        """The ``body_weight`` metric if it exists. Reads must not create it."""
        return await self.metrics.get_by_code(workspace_id, BODY_WEIGHT_CODE)

    async def weight_between(
        self, workspace_id: int, start_at: datetime, end_at: datetime
    ) -> list[HealthMeasurement]:
        rows, _ = await self.list_weight(
            workspace_id, start_at, end_at, limit=SERIES_PAGE, offset=0
        )
        return rows

    async def delete_weight(
        self,
        *,
        user_id: int,
        workspace_id: int,
        public_id: uuid.UUID,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        metric = await self._weight_metric(workspace_id)
        row = await self.measurements.get_by_public_id(workspace_id, public_id)
        if metric is None or row is None or row.metric_id != metric.id:
            raise NotFoundError(detail=f"Weight entry with id {public_id} not found")
        before = _measurement_snapshot(row, metric.code)
        await self.measurements.delete(row)
        await self._audit_measurement(
            audit_logger, user_id, workspace_id, "delete", row, before, None
        )

    async def weight_trend(
        self, workspace_id: int, days: int, user_id: int | None = None
    ) -> tuple[list[HealthMeasurement], SeriesStats]:
        """Rows inside the window (newest first) and stats whose deltas search all history."""
        metric = await self._weight_metric(workspace_id)
        if metric is None:
            return [], SeriesStats(count=0)
        history = await self.measurements.list_for_metric(workspace_id, metric.id)
        today = await self._today(user_id)
        window = [r for r in history if r.measured_on >= today - timedelta(days=days)]
        return window, compute_series_stats(window, metric, today, history_rows=history)


SERIES_PAGE = 1000


# ---------------------------------------------------------------------------
# Result shaping
# ---------------------------------------------------------------------------


def _error(message: str) -> dict[str, Any]:
    return {"status": "error", "message": message}


def _first_error(exc: PydanticValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    return f"{loc}: {first.get('msg', 'invalid')}" if loc else str(first.get("msg", "invalid"))


def _counts(plans: list[_Plan]) -> dict[str, int]:
    create_states = {"would_create", "create", "created"}
    return {
        "create": sum(1 for p in plans if p.status in create_states or p.status == "pending"),
        "duplicate": sum(1 for p in plans if p.status == "duplicate"),
        "possible_duplicate": sum(1 for p in plans if p.status == "possible_duplicate"),
        "invalid": sum(1 for p in plans if p.status == "invalid"),
        "unknown_metric": sum(1 for p in plans if p.status == "unknown_metric"),
        "would_create_metric": len({
            p.metric.code
            for p in plans
            if p.new_metric and p.metric and p.status in create_states | {"pending"}
        }),
    }


def _item_view(plan: _Plan) -> dict[str, Any]:
    return {
        "index": plan.index,
        "status": plan.status,
        "metric": plan.metric.code if plan.metric else plan.metric_label,
        "new_metric": plan.new_metric,
        "match_reason": plan.match_reason,
        "existing_id": str(plan.existing_id) if plan.existing_id else None,
        "public_id": str(plan.public_id) if plan.public_id else None,
        "errors": plan.errors,
        "warnings": plan.warnings,
        "suggestions": plan.suggestions,
    }


def weight_point_time(row: HealthMeasurement) -> datetime:
    """A tz-aware instant for a row (weight always has ``measured_at``; fall back to midnight UTC)."""
    return row.measured_at or datetime.combine(row.measured_on, time.min, tzinfo=UTC)
