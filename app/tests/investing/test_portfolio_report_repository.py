"""Unit tests for PortfolioReportRepository (spec-107).

Append-only history: exactly one row per ``(workspace_id, as_of, run_id)``.
DB-backed against the migrated testcontainer database (the session fixture
runs ``alembic upgrade head``, so this also proves the migration applies
cleanly). Each test runs inside a rolled-back transaction.
"""

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.core.database import postgres
from app.investing.models import PortfolioReport
from app.investing.repository import PortfolioReportRepository
from app.platform.models import Workspace

WS = 1
OTHER_WS = 2

_REPORT_DEFAULTS = {
    "scalars_json": {"equity_inr": "120000.00"},
    "foundation_json": {"themes": []},
    "actions_md": "# Actions\n- hold",
    "theme_weights": {"PARAG_PARIKH_FLEXI": "0.60"},
    "harvest_candidates": [],
    "lrs_headroom_inr": Decimal("45000.00"),
    "tax_buckets": {
        "lt_gain_inr": "100000.00",
        "lt_tax_inr": "12500.00",
        "st_gain_inr": "30000.00",
        "st_tax_inr": "9000.00",
        "total_tax_inr": "21500.00",
    },
}


async def _seed_workspaces(session) -> None:
    session.add(Workspace(id=WS, name="Report Workspace"))
    session.add(Workspace(id=OTHER_WS, name="Other Workspace"))
    await session.flush()


async def _create_report(
    session, workspace_id: int, as_of: date, run_id: str, **overrides
) -> PortfolioReport:
    repo = PortfolioReportRepository(session)
    data = dict(_REPORT_DEFAULTS)
    data.update(overrides)
    return await repo.create(workspace_id=workspace_id, as_of=as_of, run_id=run_id, **data)


@pytest.mark.asyncio
async def test_create_inserts_and_returns_row(override_database_url) -> None:
    async with postgres.async_session_maker() as session:
        await _seed_workspaces(session)
        report = await _create_report(session, WS, date(2026, 11, 1), "run-2026-11-01")

        assert isinstance(report.id, int) and report.id > 0
        assert isinstance(report.public_id, UUID)
        assert report.workspace_id == WS
        assert report.as_of == date(2026, 11, 1)
        assert report.run_id == "run-2026-11-01"
        assert report.theme_weights == {"PARAG_PARIKH_FLEXI": "0.60"}
        assert report.harvest_candidates == []
        assert report.lrs_headroom_inr == Decimal("45000.00")
        assert report.tax_buckets["total_tax_inr"] == "21500.00"
        assert report.created_at is not None


@pytest.mark.asyncio
async def test_create_is_append_only_idempotent(override_database_url) -> None:
    async with postgres.async_session_maker() as session:
        await _seed_workspaces(session)
        first = await _create_report(session, WS, date(2026, 11, 1), "run-2026-11-01")

        # Same (workspace_id, as_of, run_id) with different payload: no-op,
        # returns the existing row untouched.
        second = await _create_report(
            session,
            WS,
            date(2026, 11, 1),
            "run-2026-11-01",
            theme_weights={"OTHER": "1.0"},
        )
        assert second.public_id == first.public_id
        assert second.theme_weights == {"PARAG_PARIKH_FLEXI": "0.60"}
        assert second.created_at == first.created_at

        # A different run on the same date does create a new row.
        third = await _create_report(session, WS, date(2026, 11, 1), "run-2026-11-02")
        assert third.public_id != first.public_id

        count = (
            await session.execute(select(func.count()).select_from(PortfolioReport))
        ).scalar_one()
        assert count == 2


@pytest.mark.asyncio
async def test_get_by_public_id(override_database_url) -> None:
    async with postgres.async_session_maker() as session:
        await _seed_workspaces(session)
        report = await _create_report(session, WS, date(2026, 11, 1), "run-2026-11-01")

        repo = PortfolioReportRepository(session)
        got = await repo.get_by_public_id(WS, report.public_id)
        assert got is not None
        assert got.id == report.id
        assert got.run_id == "run-2026-11-01"

        # Same public_id, wrong workspace -> None.
        assert await repo.get_by_public_id(OTHER_WS, report.public_id) is None
        # Unknown public_id -> None.
        assert await repo.get_by_public_id(WS, UUID(int=0)) is None


@pytest.mark.asyncio
async def test_list_by_workspace_range_filters_and_paginates(
    override_database_url,
) -> None:
    async with postgres.async_session_maker() as session:
        await _seed_workspaces(session)
        await _create_report(session, WS, date(2026, 10, 1), "run-oct-1")
        await _create_report(session, WS, date(2026, 10, 15), "run-oct-15")
        await _create_report(session, WS, date(2026, 11, 1), "run-nov-1")
        await _create_report(session, WS, date(2026, 11, 15), "run-nov-15")
        await _create_report(session, OTHER_WS, date(2026, 11, 15), "run-nov-15")

        repo = PortfolioReportRepository(session)

        items, total = await repo.list_by_workspace_range(WS)
        assert total == 4
        assert [r.run_id for r in items] == [
            "run-nov-15",
            "run-nov-1",
            "run-oct-15",
            "run-oct-1",
        ]

        # Inclusive date range.
        items, total = await repo.list_by_workspace_range(
            WS, from_date=date(2026, 11, 1), to_date=date(2026, 11, 15)
        )
        assert total == 2
        assert {r.run_id for r in items} == {"run-nov-1", "run-nov-15"}

        # Other workspace is isolated.
        items, total = await repo.list_by_workspace_range(OTHER_WS)
        assert total == 1 and items[0].run_id == "run-nov-15"

        # Limit/offset pagination.
        items, total = await repo.list_by_workspace_range(WS, limit=2, offset=1)
        assert total == 4
        assert [r.run_id for r in items] == ["run-nov-1", "run-oct-15"]

        # Empty range.
        items, total = await repo.list_by_workspace_range(
            WS, from_date=date(2026, 12, 1), to_date=date(2026, 12, 31)
        )
        assert total == 0 and items == []
