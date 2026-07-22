"""Sibling of app/tests/investing/test_snapshot_repository_race.py: the same
check-then-insert race exists in NetWorthSnapshotRepository.upsert() against
uq_workspace_net_worth_snapshot_day, reachable the same way (an unlocked
dashboard/net-worth GET racing net_worth_snapshot_job for the same
workspace_id, snapshot_date). No prod incident reported for this one yet, but
it shares the exact defect shape as the reported portfolio_snapshots one.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.finance.models import NetWorthSnapshot
from app.finance.repository import NetWorthSnapshotRepository
from app.platform.models import Workspace


@pytest.mark.asyncio
async def test_upsert_survives_concurrent_first_write_for_same_day(
    test_database_engine: AsyncEngine,
) -> None:
    async with AsyncSession(test_database_engine, expire_on_commit=False) as setup_session:
        workspace = Workspace(name="net-worth-race-test-workspace")
        setup_session.add(workspace)
        await setup_session.commit()
        workspace_id = workspace.id

    snapshot_date = datetime.now(UTC).date()

    def _snapshot(total: Decimal) -> NetWorthSnapshot:
        return NetWorthSnapshot(
            workspace_id=workspace_id,
            snapshot_date=snapshot_date,
            reporting_currency="USD",
            holdings_value=total,
            investing_cash=Decimal("0.00"),
            spending_cash=Decimal("0.00"),
            total_net_worth=total,
            fx_rates_used={},
        )

    try:

        async def _upsert(total: Decimal) -> None:
            async with AsyncSession(test_database_engine, expire_on_commit=False) as session:
                await NetWorthSnapshotRepository(session).upsert(_snapshot(total))
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
                        select(NetWorthSnapshot).where(
                            NetWorthSnapshot.workspace_id == workspace_id,
                            NetWorthSnapshot.snapshot_date == snapshot_date,
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
                delete(NetWorthSnapshot).where(NetWorthSnapshot.workspace_id == workspace_id)
            )
            await cleanup_session.execute(delete(Workspace).where(Workspace.id == workspace_id))
            await cleanup_session.commit()
