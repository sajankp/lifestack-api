"""Regression test for a real production incident (PostHog, 2026-07-18 and
2026-07-19): two unlocked callers of PerformanceService.summary() -- e.g. a
user opening the dashboard at the same moment morning_briefing_job runs --
both discover no PortfolioSnapshot exists yet for today and both attempt to
create it, racing PortfolioSnapshotRepository.upsert()'s check-then-insert
and raising IntegrityError on uq_snapshot_workspace_date.

This test needs two genuinely independent, concurrently-committing DB
transactions to reproduce the race, so it bypasses the `client` fixture's
single-connection/rollback-per-test isolation (that fixture never actually
commits, so a second independent connection can't see its writes) and talks
to the session-scoped test engine directly, cleaning up its own rows after.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.investing.models import PortfolioSnapshot
from app.investing.repository import PortfolioSnapshotRepository
from app.platform.models import Workspace


@pytest.mark.asyncio
async def test_upsert_survives_concurrent_first_write_for_same_day(
    test_database_engine: AsyncEngine,
) -> None:
    async with AsyncSession(test_database_engine, expire_on_commit=False) as setup_session:
        workspace = Workspace(name="snapshot-race-test-workspace")
        setup_session.add(workspace)
        await setup_session.commit()
        workspace_id = workspace.id

    snapshot_date = datetime.now(UTC).date()

    def _snapshot(total: Decimal) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            workspace_id=workspace_id,
            snapshot_date=snapshot_date,
            total_value=total,
            total_cost=total,
            holdings_value=total,
            cash_value=Decimal("0.00"),
            currency_code="USD",
            fx_rates_used={},
        )

    try:

        async def _upsert(total: Decimal) -> None:
            async with AsyncSession(test_database_engine, expire_on_commit=False) as session:
                await PortfolioSnapshotRepository(session).upsert(_snapshot(total))
                await session.commit()

        results = await asyncio.gather(
            _upsert(Decimal("100.00")), _upsert(Decimal("200.00")), return_exceptions=True
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, f"concurrent upsert raised: {errors!r}"

        async with AsyncSession(test_database_engine, expire_on_commit=False) as verify_session:
            rows = (
                (
                    await verify_session.execute(
                        select(PortfolioSnapshot).where(
                            PortfolioSnapshot.workspace_id == workspace_id,
                            PortfolioSnapshot.snapshot_date == snapshot_date,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
    finally:
        async with AsyncSession(test_database_engine, expire_on_commit=False) as cleanup_session:
            await cleanup_session.execute(
                delete(PortfolioSnapshot).where(PortfolioSnapshot.workspace_id == workspace_id)
            )
            await cleanup_session.execute(delete(Workspace).where(Workspace.id == workspace_id))
            await cleanup_session.commit()
