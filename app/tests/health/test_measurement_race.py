"""Concurrent identical bulk writes leave exactly one copy (spec-111 idempotency)."""

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.auth.models import User
from app.health.measurements.models import HealthMeasurement, HealthRecord
from app.health.measurements.schemas import MetricDefinition
from app.health.measurements.service import MeasurementService
from app.platform.models import Workspace

pytestmark = pytest.mark.asyncio

WS = 9301
USER = 9301


async def _seed(engine: AsyncEngine) -> None:
    async with AsyncSession(engine) as session:
        session.add_all([
            User(id=USER, email="race-h@example.com", username="race_h", hashed_password="x"),
            Workspace(id=WS, name="Race"),
        ])
        await session.flush()
        service = MeasurementService.from_session(session)
        await service.define_metric(
            user_id=USER,
            workspace_id=WS,
            definition=MetricDefinition(
                name="HbA1c", code="hba1c", category="blood", canonical_unit="%"
            ),
        )
        await session.commit()


async def _send(engine: AsyncEngine, record: dict, items: list[dict], **kw) -> dict:
    async with AsyncSession(engine) as session:
        out = await MeasurementService.from_session(session).record_results(
            user_id=USER, workspace_id=WS, record=record, items=items, dry_run=False, **kw
        )
        await session.commit()
        return out


async def _counts(engine: AsyncEngine) -> tuple[int, int]:
    async with AsyncSession(engine) as session:
        records = (await session.execute(select(func.count(HealthRecord.id)))).scalar_one()
        rows = (await session.execute(select(func.count(HealthMeasurement.id)))).scalar_one()
        return int(records), int(rows)


async def test_concurrent_resends_of_one_report_create_one_record(race_engine: AsyncEngine):
    await _seed(race_engine)
    record = {"title": "Race panel", "recorded_on": "2026-11-10", "source_ref": "race-report-1"}
    items = [
        {"metric": "hba1c", "value": "5.6", "unit": "%"},
        {"metric": "hba1c", "value": "5.9", "unit": "%", "measured_on": "2026-11-09"},
    ]
    results = await asyncio.gather(*[_send(race_engine, record, items) for _ in range(4)])
    assert all(r["status"] == "success" for r in results), results
    assert sum(r["counts"]["create"] for r in results) == 2
    assert sum(1 for r in results if r["record"]["created"]) == 1
    assert await _counts(race_engine) == (1, 2)


async def test_concurrent_natural_key_batches_respect_multiplicity(race_engine: AsyncEngine):
    """Three simultaneous retries of one 2-reading batch (no source_ref) leave exactly 2."""
    await _seed(race_engine)
    item = {"metric": "hba1c", "value": "5.6", "unit": "%", "measured_on": "2026-11-10"}
    results = await asyncio.gather(*[
        _send(race_engine, {"title": "Retry"}, [item, item], skip_duplicates=True) for _ in range(3)
    ])
    assert all(r["status"] == "success" for r in results), results
    assert sum(r["counts"]["create"] for r in results) == 2
    assert (await _counts(race_engine))[1] == 2
