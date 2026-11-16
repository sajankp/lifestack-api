"""Copy ``weight_entries`` into ``health_measurements`` as ``body_weight`` readings (spec-111 §D).

A reviewed one-time repair, not a migration (the repo rule: alembic is schema only). It is
idempotent, refuses when a previously copied row no longer matches its source, and leaves
``weight_entries`` untouched so it stays the rollback path. Run through
``scripts/migrate_weight_to_health_measurements.py``.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.health.measurements.models import HealthMeasurement
from app.health.measurements.service import MeasurementService
from app.health.models import WeightEntry

_UNIT = "kg"


class MigrationRefusedError(RuntimeError):
    """The copied data no longer matches what was reviewed; nothing was written."""


@dataclass(frozen=True)
class WorkspacePlan:
    workspace_id: int
    source_count: int
    already_copied: int
    to_copy: int
    latest_source: tuple[Decimal, datetime] | None


@dataclass(frozen=True)
class WorkspaceResult:
    workspace_id: int
    source_count: int
    copied: int
    target_count: int
    extra_target: int  # weights written to the new store after the flip (no source row)
    latest: tuple[Decimal, datetime] | None


async def _workspace_ids(session: AsyncSession, workspace_id: int | None) -> list[int]:
    stmt = select(WeightEntry.workspace_id).distinct().order_by(WeightEntry.workspace_id)
    if workspace_id is not None:
        stmt = stmt.where(WeightEntry.workspace_id == workspace_id)
    return [w for (w,) in (await session.execute(stmt)).all()]


async def _sources(session: AsyncSession, workspace_id: int) -> list[WeightEntry]:
    result = await session.execute(
        select(WeightEntry).where(WeightEntry.workspace_id == workspace_id).order_by(WeightEntry.id)
    )
    return list(result.scalars().all())


async def _existing_targets(
    session: AsyncSession, sources: list[WeightEntry]
) -> dict[object, HealthMeasurement]:
    """Targets already carrying a source row's public id, from ANY workspace."""
    ids = [s.public_id for s in sources]
    if not ids:
        return {}
    result = await session.execute(
        select(HealthMeasurement).where(HealthMeasurement.public_id.in_(ids))
    )
    return {row.public_id: row for row in result.scalars().all()}


def _check_unchanged(source: WeightEntry, target: HealthMeasurement, metric_id: int | None) -> None:
    if target.workspace_id != source.workspace_id:
        raise MigrationRefusedError(
            f"Measurement {source.public_id} exists in another workspace; refusing to continue."
        )
    if metric_id is not None and target.metric_id != metric_id:
        raise MigrationRefusedError(
            f"Measurement {source.public_id} exists under a different metric; refusing to continue."
        )
    same = (
        target.user_id == source.user_id
        and target.measured_at == source.measured_at
        and target.value_num == source.weight_kg
        and target.note == source.note
    )
    if not same:
        raise MigrationRefusedError(
            f"Copied measurement {source.public_id} differs from its source row "
            f"(value/time/note/user changed since the copy); refusing to continue."
        )


async def _metric_id(session: AsyncSession, workspace_id: int, create: bool) -> int | None:
    service = MeasurementService.from_session(session)
    if create:
        return (await service.ensure_builtin_metrics(workspace_id)).id
    metric = await service.metrics.get_by_code(workspace_id, "body_weight")
    return metric.id if metric is not None else None


async def plan_weight_migration(
    session: AsyncSession, workspace_id: int | None = None
) -> list[WorkspacePlan]:
    """Read-only: what ``apply_weight_migration`` would copy. Raises on drift."""
    plans: list[WorkspacePlan] = []
    for ws in await _workspace_ids(session, workspace_id):
        sources = await _sources(session, ws)
        metric_id = await _metric_id(session, ws, create=False)
        existing = await _existing_targets(session, sources)
        for source in sources:
            if source.public_id in existing:
                _check_unchanged(source, existing[source.public_id], metric_id)
        newest = max(sources, key=lambda s: (s.measured_at, s.id)) if sources else None
        plans.append(
            WorkspacePlan(
                workspace_id=ws,
                source_count=len(sources),
                already_copied=len(existing),
                to_copy=len(sources) - len(existing),
                latest_source=(newest.weight_kg, newest.measured_at) if newest else None,
            )
        )
    return plans


async def apply_weight_migration(
    session: AsyncSession, workspace_id: int | None = None
) -> list[WorkspaceResult]:
    """Copy the missing rows. Everything is checked before anything is written."""
    await plan_weight_migration(session, workspace_id)  # refuses on drift, writes nothing
    results: list[WorkspaceResult] = []
    for ws in await _workspace_ids(session, workspace_id):
        sources = await _sources(session, ws)
        metric_id = await _metric_id(session, ws, create=True)
        assert metric_id is not None
        existing = await _existing_targets(session, sources)
        zones = await _timezones(session, {s.user_id for s in sources})

        copied = 0
        for source in sources:
            if source.public_id in existing:
                continue
            tz = zones.get(source.user_id, ZoneInfo("UTC"))
            session.add(
                HealthMeasurement(
                    public_id=source.public_id,
                    workspace_id=source.workspace_id,
                    user_id=source.user_id,
                    metric_id=metric_id,
                    measured_on=source.measured_at.astimezone(tz).date(),
                    measured_at=source.measured_at,
                    value_num=source.weight_kg,
                    unit=_UNIT,
                    note=source.note,
                    source_type=source.source_type,
                    source_ref=source.source_ref,
                    source_import_id=source.source_import_id,
                    created_at=source.created_at,
                    updated_at=source.updated_at,
                )
            )
            copied += 1
        await session.flush()

        target_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(HealthMeasurement)
                    .where(
                        HealthMeasurement.workspace_id == ws,
                        HealthMeasurement.metric_id == metric_id,
                    )
                )
            ).scalar_one()
        )
        present = await _existing_targets(session, sources)
        if len(present) != len(sources):
            raise MigrationRefusedError(
                f"Workspace {ws}: {len(sources) - len(present)} source row(s) are missing after the copy."
            )
        newest = max(sources, key=lambda s: (s.measured_at, s.id)) if sources else None
        results.append(
            WorkspaceResult(
                workspace_id=ws,
                source_count=len(sources),
                copied=copied,
                target_count=target_count,
                extra_target=target_count - len(sources),
                latest=(newest.weight_kg, newest.measured_at) if newest else None,
            )
        )
    return results


async def _timezones(session: AsyncSession, user_ids: set[int]) -> dict[int, ZoneInfo]:
    if not user_ids:
        return {}
    rows = (
        await session.execute(select(User.id, User.timezone).where(User.id.in_(user_ids)))
    ).all()
    zones: dict[int, ZoneInfo] = {}
    for user_id, name in rows:
        try:
            zones[user_id] = ZoneInfo(name or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            zones[user_id] = ZoneInfo("UTC")
    return zones
