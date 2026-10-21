"""Regression test for the concurrent-order race on the holding insert.

Two order placements for the same symbol and account issued at the same
time used to let both callers find no existing holding and both INSERT,
raising IntegrityError on uq_holding_workspace_symbol_account and
returning 500 to one of them.

The holding write is now an ON CONFLICT DO UPDATE upsert, so it is
idempotent under concurrency. This mirrors
tests/investing/test_snapshot_repository_race.py, which covers the same
class of bug for PortfolioSnapshotRepository.upsert.

This test needs two genuinely independent, concurrently-committing DB
transactions to reproduce the race, so it bypasses the `client` fixture's
single-connection/rollback-per-test isolation (that fixture never
actually commits, so a second independent connection cannot see its
writes) and talks to the session-scoped test engine directly, cleaning up
its own rows after.
"""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.auth.models import User
from app.finance.models import Account
from app.investing.models import Holding
from app.investing.repository import HoldingRepository
from app.platform.models import Workspace


@pytest.mark.asyncio
async def test_holding_upsert_survives_concurrent_first_write(
    test_database_engine: AsyncEngine,
) -> None:
    async with AsyncSession(test_database_engine, expire_on_commit=False) as setup_session:
        user = User(
            email=f"holding-race-{uuid.uuid4().hex[:8]}@example.com",
            username=f"holding_race_{uuid.uuid4().hex[:8]}",
            hashed_password="not-a-real-hash",
        )
        setup_session.add(user)
        await setup_session.commit()
        user_id = user.id

        workspace = Workspace(name="holding-race-test-workspace")
        setup_session.add(workspace)
        await setup_session.commit()
        workspace_id = workspace.id

        account = Account(
            workspace_id=workspace_id,
            name="race brokerage",
            account_type="brokerage",
            default_currency_code="USD",
        )
        setup_session.add(account)
        await setup_session.commit()
        account_id = account.id

    def _holding(quantity: str, avg_cost: str) -> Holding:
        return Holding(
            workspace_id=workspace_id,
            user_id=user_id,
            symbol="AAPL",
            account_id=account_id,
            quantity=Decimal(quantity),
            avg_cost=Decimal(avg_cost),
            currency="USD",
            source_type="order",
        )

    try:

        async def _upsert(quantity: str, avg_cost: str) -> None:
            async with AsyncSession(test_database_engine, expire_on_commit=False) as session:
                await HoldingRepository(session).upsert(_holding(quantity, avg_cost))
                await session.commit()

        results = await asyncio.gather(
            _upsert("10.00000000", "150.000000"),
            _upsert("5.00000000", "170.000000"),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, f"concurrent holding upsert raised: {errors!r}"

        async with AsyncSession(test_database_engine, expire_on_commit=False) as verify_session:
            rows = (
                (
                    await verify_session.execute(
                        select(Holding).where(
                            Holding.workspace_id == workspace_id,
                            Holding.symbol == "AAPL",
                            Holding.account_id == account_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        # Exactly one holding row, and it is not zero.
        assert len(rows) == 1
        assert rows[0].quantity > 0
    finally:
        async with AsyncSession(test_database_engine, expire_on_commit=False) as cleanup_session:
            await cleanup_session.execute(
                delete(Holding).where(Holding.workspace_id == workspace_id)
            )
            await cleanup_session.execute(
                delete(Account).where(Account.workspace_id == workspace_id)
            )
            await cleanup_session.execute(delete(User).where(User.id == user_id))
            await cleanup_session.execute(delete(Workspace).where(Workspace.id == workspace_id))
            await cleanup_session.commit()
