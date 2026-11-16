from collections.abc import Sequence
from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.auth.models import User
from app.core.repository import BaseRepository
from app.health.measurements.models import HealthMeasurement, HealthMetric, HealthRecord

# A series read is bounded; spec-110 sizes (tens of rows per metric) are far below this.
SERIES_ROW_CAP = 5000
_WRITE_LOCK_NAMESPACE = 1_212_501_320  # distinct from the other advisory-lock namespaces


class MetricRepository(BaseRepository[HealthMetric]):
    async def list_for_workspace(self, workspace_id: int) -> Sequence[HealthMetric]:
        result = await self.session.execute(
            select(HealthMetric)
            .where(HealthMetric.workspace_id == workspace_id)
            .order_by(HealthMetric.category, HealthMetric.name)
        )
        return result.scalars().all()

    async def get_by_code(self, workspace_id: int, code: str) -> HealthMetric | None:
        result = await self.session.execute(
            select(HealthMetric).where(
                HealthMetric.workspace_id == workspace_id, HealthMetric.code == code
            )
        )
        return result.scalar_one_or_none()

    async def insert_ignore_conflict(self, workspace_id: int, values: dict) -> HealthMetric | None:
        """Race-safe create: returns None when the (workspace, code) already exists."""
        stmt = (
            pg_insert(HealthMetric)
            .values(workspace_id=workspace_id, **values)
            .on_conflict_do_nothing(constraint="uq_health_metrics_workspace_code")
            .returning(HealthMetric.id)
        )
        new_id = (await self.session.execute(stmt)).scalar_one_or_none()
        if new_id is None:
            return None
        created = await self.session.get(HealthMetric, new_id)
        await self.session.refresh(created)
        return created

    async def measurement_counts(self, workspace_id: int) -> dict[int, int]:
        result = await self.session.execute(
            select(HealthMeasurement.metric_id, func.count(HealthMeasurement.id))
            .where(HealthMeasurement.workspace_id == workspace_id)
            .group_by(HealthMeasurement.metric_id)
        )
        return {metric_id: int(count) for metric_id, count in result.all()}


class RecordRepository(BaseRepository[HealthRecord]):
    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> HealthRecord | None:
        result = await self.session.execute(
            select(HealthRecord).where(
                HealthRecord.workspace_id == workspace_id, HealthRecord.public_id == public_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_source_ref(self, workspace_id: int, source_ref: str) -> HealthRecord | None:
        result = await self.session.execute(
            select(HealthRecord).where(
                HealthRecord.workspace_id == workspace_id, HealthRecord.source_ref == source_ref
            )
        )
        return result.scalar_one_or_none()

    async def get_by_ids(self, workspace_id: int, ids: Sequence[int]) -> dict[int, HealthRecord]:
        unique = {i for i in ids if i is not None}
        if not unique:
            return {}
        result = await self.session.execute(
            select(HealthRecord).where(
                HealthRecord.workspace_id == workspace_id, HealthRecord.id.in_(unique)
            )
        )
        return {r.id: r for r in result.scalars().all() if r.id is not None}

    async def list_for_workspace(
        self,
        workspace_id: int,
        kind: str | None = None,
        from_on: date | None = None,
        to_on: date | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[Sequence[HealthRecord], int]:
        base = select(HealthRecord).where(HealthRecord.workspace_id == workspace_id)
        if kind is not None:
            base = base.where(HealthRecord.kind == kind)
        if from_on is not None:
            base = base.where(HealthRecord.recorded_on >= from_on)
        if to_on is not None:
            base = base.where(HealthRecord.recorded_on <= to_on)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        rows = await self.session.execute(
            base
            .order_by(HealthRecord.recorded_on.desc(), HealthRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return rows.scalars().all(), int(total)

    async def measurement_counts(self, record_ids: Sequence[int]) -> dict[int, int]:
        if not record_ids:
            return {}
        result = await self.session.execute(
            select(HealthMeasurement.record_id, func.count(HealthMeasurement.id))
            .where(HealthMeasurement.record_id.in_(record_ids))
            .group_by(HealthMeasurement.record_id)
        )
        return {record_id: int(count) for record_id, count in result.all()}

    async def lock_workspace_writes(self, workspace_id: int) -> None:
        """Serialise bulk writes per workspace until this transaction ends.

        Identical concurrent batches (client retries) must see each other's committed rows
        when they dedupe, or both would insert.
        """
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :ws)"),
            {"ns": _WRITE_LOCK_NAMESPACE, "ws": workspace_id},
        )


class MeasurementRepository(BaseRepository[HealthMeasurement]):
    async def get_by_public_id(
        self, workspace_id: int, public_id: UUID
    ) -> HealthMeasurement | None:
        result = await self.session.execute(
            select(HealthMeasurement).where(
                HealthMeasurement.workspace_id == workspace_id,
                HealthMeasurement.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_source_ref(
        self, workspace_id: int, source_ref: str
    ) -> HealthMeasurement | None:
        result = await self.session.execute(
            select(HealthMeasurement).where(
                HealthMeasurement.workspace_id == workspace_id,
                HealthMeasurement.source_ref == source_ref,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_metric(
        self,
        workspace_id: int,
        metric_id: int,
        *,
        from_on: date | None = None,
        to_on: date | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> list[HealthMeasurement]:
        """Newest first by the ordering key (measured_on, measured_at NULLS FIRST, id)."""
        query = select(HealthMeasurement).where(
            HealthMeasurement.workspace_id == workspace_id,
            HealthMeasurement.metric_id == metric_id,
        )
        if from_on is not None:
            query = query.where(HealthMeasurement.measured_on >= from_on)
        if to_on is not None:
            query = query.where(HealthMeasurement.measured_on <= to_on)
        if start_at is not None:
            query = query.where(HealthMeasurement.measured_at >= start_at)
        if end_at is not None:
            query = query.where(HealthMeasurement.measured_at <= end_at)
        query = query.order_by(
            HealthMeasurement.measured_on.desc(),
            HealthMeasurement.measured_at.desc().nulls_last(),
            HealthMeasurement.id.desc(),
        ).limit(SERIES_ROW_CAP)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def list_for_record(self, record_id: int) -> list[HealthMeasurement]:
        result = await self.session.execute(
            select(HealthMeasurement)
            .where(HealthMeasurement.record_id == record_id)
            .order_by(HealthMeasurement.id.asc())
        )
        return list(result.scalars().all())

    async def find_for_dedupe(
        self, workspace_id: int, metric_ids: Sequence[int], days: Sequence[date]
    ) -> list[HealthMeasurement]:
        """Existing rows that could be the same reading as a batch item."""
        if not metric_ids or not days:
            return []
        result = await self.session.execute(
            select(HealthMeasurement).where(
                HealthMeasurement.workspace_id == workspace_id,
                HealthMeasurement.metric_id.in_(set(metric_ids)),
                HealthMeasurement.measured_on.in_(set(days)),
            )
        )
        return list(result.scalars().all())

    async def latest_per_metric(self, workspace_id: int) -> dict[int, HealthMeasurement]:
        result = await self.session.execute(
            select(HealthMeasurement)
            .where(HealthMeasurement.workspace_id == workspace_id)
            .order_by(
                HealthMeasurement.metric_id,
                HealthMeasurement.measured_on.desc(),
                HealthMeasurement.measured_at.desc().nulls_last(),
                HealthMeasurement.id.desc(),
            )
        )
        latest: dict[int, HealthMeasurement] = {}
        for row in result.scalars().all():
            latest.setdefault(row.metric_id, row)
        return latest

    async def count_for_metric(self, workspace_id: int, metric_id: int) -> int:
        result = await self.session.execute(
            select(func.count(HealthMeasurement.id)).where(
                HealthMeasurement.workspace_id == workspace_id,
                HealthMeasurement.metric_id == metric_id,
            )
        )
        return int(result.scalar() or 0)

    async def distinct_units(self, workspace_id: int, metric_id: int) -> list[str]:
        result = await self.session.execute(
            select(HealthMeasurement.unit)
            .where(
                HealthMeasurement.workspace_id == workspace_id,
                HealthMeasurement.metric_id == metric_id,
                HealthMeasurement.unit.is_not(None),
            )
            .distinct()
        )
        return [u for (u,) in result.all() if u]

    async def delete_for_record(self, record_id: int) -> int:
        result = await self.session.execute(
            delete(HealthMeasurement).where(HealthMeasurement.record_id == record_id)
        )
        return int(result.rowcount or 0)

    async def get_user_timezone(self, user_id: int) -> ZoneInfo:
        """The user's IANA timezone; UTC when unset or unknown."""
        name = (
            await self.session.execute(select(User.timezone).where(User.id == user_id))
        ).scalar_one_or_none()
        try:
            return ZoneInfo(name or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")
