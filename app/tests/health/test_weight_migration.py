"""One-time copy of ``weight_entries`` into ``health_measurements`` (spec-111 §D)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.auth.models import User
from app.core.database import postgres
from app.health.measurements.models import HealthMeasurement, HealthMetric
from app.health.measurements.service import MeasurementService
from app.health.measurements.weight_migration import (
    MigrationRefusedError,
    apply_weight_migration,
    plan_weight_migration,
)
from app.health.models import WeightEntry
from app.platform.models import Workspace

WS_A = 9501
WS_B = 9502
USER_A = 9501
USER_B = 9502

pytestmark = pytest.mark.asyncio


async def _seed(session) -> list[WeightEntry]:
    session.add_all([
        User(
            id=USER_A,
            email="wm-a@example.com",
            username="wm_a",
            hashed_password="x",
            timezone="Asia/Kolkata",
        ),
        User(id=USER_B, email="wm-b@example.com", username="wm_b", hashed_password="x"),
        Workspace(id=WS_A, name="A"),
        Workspace(id=WS_B, name="B"),
    ])
    await session.flush()
    created = datetime(2026, 3, 5, 6, 0, tzinfo=UTC)
    rows = [
        # 20:00 UTC on 1 March is 01:30 IST on 2 March: the local day must be the 2nd.
        WeightEntry(
            workspace_id=WS_A,
            user_id=USER_A,
            measured_at=datetime(2026, 3, 1, 20, 0, tzinfo=UTC),
            weight_kg=Decimal("80.25"),
            note="after trip",
            created_at=created,
            updated_at=created,
        ),
        WeightEntry(
            workspace_id=WS_A,
            user_id=USER_A,
            measured_at=datetime(2026, 3, 8, 6, 0, tzinfo=UTC),
            weight_kg=Decimal("79.50"),
            created_at=created,
            updated_at=created,
        ),
        WeightEntry(
            workspace_id=WS_B,
            user_id=USER_B,
            measured_at=datetime(2026, 3, 3, 7, 0, tzinfo=UTC),
            weight_kg=Decimal("65.00"),
            source_ref="scale-1",
            created_at=created,
            updated_at=created,
        ),
    ]
    session.add_all(rows)
    await session.flush()
    return rows


async def _target_count(session, workspace_id: int | None = None) -> int:
    stmt = select(func.count()).select_from(HealthMeasurement)
    if workspace_id is not None:
        stmt = stmt.where(HealthMeasurement.workspace_id == workspace_id)
    return int((await session.execute(stmt)).scalar_one())


async def test_dry_run_reports_and_writes_nothing(override_database_url):
    async with postgres.async_session_maker() as session:
        await _seed(session)
        plans = await plan_weight_migration(session)
        by_ws = {p.workspace_id: p for p in plans}
        assert by_ws[WS_A].source_count == 2 and by_ws[WS_A].to_copy == 2
        assert by_ws[WS_B].source_count == 1
        assert by_ws[WS_A].latest_source == (
            Decimal("79.50"),
            datetime(2026, 3, 8, 6, 0, tzinfo=UTC),
        )
        assert await _target_count(session) == 0
        assert (
            await session.execute(select(func.count()).select_from(HealthMetric))
        ).scalar_one() == 0


async def test_apply_copies_every_row_preserving_identity_and_local_day(override_database_url):
    async with postgres.async_session_maker() as session:
        rows = await _seed(session)
        results = await apply_weight_migration(session)
        assert {r.workspace_id: r.copied for r in results} == {WS_A: 2, WS_B: 1}

        for src in rows:
            dst = (
                await session.execute(
                    select(HealthMeasurement).where(HealthMeasurement.public_id == src.public_id)
                )
            ).scalar_one()
            assert dst.workspace_id == src.workspace_id and dst.user_id == src.user_id
            assert dst.value_num == src.weight_kg and dst.unit == "kg"
            assert dst.measured_at == src.measured_at
            assert dst.note == src.note and dst.source_ref == src.source_ref
            assert dst.source_type == src.source_type == "manual"
            assert dst.created_at == src.created_at and dst.updated_at == src.updated_at
            assert dst.record_id is None
        first = (
            await session.execute(
                select(HealthMeasurement).where(HealthMeasurement.public_id == rows[0].public_id)
            )
        ).scalar_one()
        assert first.measured_on.isoformat() == "2026-03-02"  # IST, not the UTC date
        user_b_row = (
            await session.execute(
                select(HealthMeasurement).where(HealthMeasurement.public_id == rows[2].public_id)
            )
        ).scalar_one()
        assert user_b_row.measured_on.isoformat() == "2026-03-03"  # no timezone set: UTC

        # The old table is untouched: it is the rollback path.
        assert (
            await session.execute(select(func.count()).select_from(WeightEntry))
        ).scalar_one() == 3


async def test_the_copied_rows_read_back_through_the_weight_api(override_database_url):
    async with postgres.async_session_maker() as session:
        rows = await _seed(session)
        await apply_weight_migration(session)
        listed, total = await MeasurementService.from_session(session).list_weight(WS_A)
        assert total == 2
        assert [r.public_id for r in listed] == [rows[1].public_id, rows[0].public_id]


async def test_second_run_is_a_noop_and_a_later_run_sweeps_new_old_rows(override_database_url):
    async with postgres.async_session_maker() as session:
        await _seed(session)
        await apply_weight_migration(session)
        again = await apply_weight_migration(session)
        assert [r.copied for r in again] == [0, 0]
        assert await _target_count(session) == 3

        # A row written to the old table between the first run and the deploy is swept up.
        late = WeightEntry(
            workspace_id=WS_A,
            user_id=USER_A,
            measured_at=datetime(2026, 3, 9, 6, 0, tzinfo=UTC),
            weight_kg=Decimal("79.00"),
        )
        session.add(late)
        await session.flush()
        sweep = await apply_weight_migration(session)
        assert {r.workspace_id: r.copied for r in sweep}[WS_A] == 1
        assert await _target_count(session, WS_A) == 3


async def test_rows_written_after_the_flip_are_tolerated(override_database_url):
    """New weights land only in the new table: more target rows than source rows is fine."""
    async with postgres.async_session_maker() as session:
        await _seed(session)
        await apply_weight_migration(session)
        await MeasurementService.from_session(session).log_weight(
            user_id=USER_A,
            workspace_id=WS_A,
            weight_kg=Decimal("78.00"),
            measured_at=datetime(2026, 3, 20, 6, 0, tzinfo=UTC),
            note=None,
        )
        results = await apply_weight_migration(session)
        a = next(r for r in results if r.workspace_id == WS_A)
        assert (a.copied, a.source_count, a.target_count, a.extra_target) == (0, 2, 3, 1)


async def test_drift_refuses_and_writes_nothing_in_any_workspace(override_database_url):
    async with postgres.async_session_maker() as session:
        rows = await _seed(session)
        await apply_weight_migration(session, workspace_id=WS_A)
        # Someone edited the copied row: the reviewed state no longer matches.
        copied = (
            await session.execute(
                select(HealthMeasurement).where(HealthMeasurement.public_id == rows[0].public_id)
            )
        ).scalar_one()
        copied.value_num = Decimal("81.00")
        await session.flush()

        with pytest.raises(MigrationRefusedError, match="differs"):
            await apply_weight_migration(session)
        assert await _target_count(session, WS_B) == 0  # workspace B was not copied either


async def test_public_id_collision_with_a_foreign_row_refuses(override_database_url):
    async with postgres.async_session_maker() as session:
        rows = await _seed(session)
        metric = await MeasurementService.from_session(session).ensure_builtin_metrics(WS_B)
        session.add(
            HealthMeasurement(
                public_id=rows[0].public_id,
                workspace_id=WS_B,
                user_id=USER_B,
                metric_id=metric.id,
                measured_on=datetime(2026, 3, 1).date(),
                value_num=Decimal("1"),
                unit="kg",
            )
        )
        await session.flush()
        with pytest.raises(MigrationRefusedError, match="another workspace"):
            await apply_weight_migration(session)


async def test_workspace_filter_only_touches_that_workspace(override_database_url):
    async with postgres.async_session_maker() as session:
        await _seed(session)
        await apply_weight_migration(session, workspace_id=WS_B)
        assert await _target_count(session, WS_A) == 0
        assert await _target_count(session, WS_B) == 1


async def test_a_workspace_with_no_weights_is_skipped(override_database_url):
    async with postgres.async_session_maker() as session:
        session.add_all([Workspace(id=WS_A, name="empty")])
        await session.flush()
        assert await apply_weight_migration(session) == []
