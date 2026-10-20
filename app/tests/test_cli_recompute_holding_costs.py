"""Tests for the `recompute_holding_costs` CLI job (PR #246 repair).

The job was merged with a commit message claiming "All 17 CLI tests pass",
but no test exercised it: the 17 were the pre-existing `test_cli.py` cases,
which only assert that registered jobs are callable. The job itself raised
`ImportError` on a non-existent `get_async_session` helper, and then
`TypeError` because `_recompute_holding` is an instance method.

These tests exercise the job end to end against the real FIFO replay so a
regression of that class cannot ship again.
"""

import sys
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.auth.models import User
from app.cli.run import JOBS, main, recompute_holding_costs_job
from app.core.database import postgres
from app.finance.models import Account
from app.investing.models import Holding, InvestingOrder
from app.platform.models import Workspace, WorkspaceMembership

WORKSPACE_ID = 9401
USER_ID = 9401
ACCOUNT_ID = 9401


async def _seed_workspace(session) -> None:
    """Workspace + owner membership + brokerage account.

    USD is seeded by migration 0008, so it is not inserted here.
    """
    session.add(
        User(
            id=USER_ID,
            email="recompute-costs@example.com",
            username="recompute_costs",
            hashed_password="hashed_password_here",
        )
    )
    session.add(Workspace(id=WORKSPACE_ID, name="recompute-costs-ws"))
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=WORKSPACE_ID, user_id=USER_ID, role="owner"))
    await session.flush()
    session.add(
        Account(
            id=ACCOUNT_ID,
            workspace_id=WORKSPACE_ID,
            name="recompute-brokerage",
            account_type="brokerage",
            default_currency_code="USD",
        )
    )
    await session.flush()


def _order(
    *,
    order_type: str,
    quantity: str,
    price: str,
    occurred_at: datetime,
) -> InvestingOrder:
    qty = Decimal(quantity)
    unit = Decimal(price)
    gross = qty * unit
    return InvestingOrder(
        workspace_id=WORKSPACE_ID,
        user_id=USER_ID,
        account_id=ACCOUNT_ID,
        order_type=order_type,
        symbol="MU",
        quantity=qty,
        price_per_unit=unit,
        gross_amount=gross,
        brokerage_fee=Decimal("0"),
        tax_amount=Decimal("0"),
        other_fees=Decimal("0"),
        net_amount=gross,
        currency="USD",
        occurred_at=occurred_at,
        source_type="manual",
    )


async def _seed_two_buy_lots(session) -> None:
    """Two buys of the same symbol, plus a holding with a deliberately wrong
    avg_cost. FIFO cost basis for 10 @ 100 then 10 @ 200 is 150.00/unit."""
    await _seed_workspace(session)

    session.add(
        _order(
            order_type="buy",
            quantity="10",
            price="100",
            occurred_at=datetime(2026, 1, 5, tzinfo=UTC),
        )
    )
    session.add(
        _order(
            order_type="buy",
            quantity="10",
            price="200",
            occurred_at=datetime(2026, 2, 5, tzinfo=UTC),
        )
    )
    session.add(
        Holding(
            workspace_id=WORKSPACE_ID,
            user_id=USER_ID,
            symbol="MU",
            account_id=ACCOUNT_ID,
            quantity=Decimal("20"),
            # Deliberately wrong: a naive average of 100 and 200 is also 150,
            # so use a value only FIFO can produce, proving the replay ran.
            avg_cost=Decimal("999.000000"),
            currency="USD",
            source_type="order",
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_recompute_job_replays_fifo_and_writes_avg_cost(override_database_url):
    """The job must rebuild avg_cost from the order history via FIFO."""
    async with postgres.async_session_maker() as session, session.begin():
        await _seed_two_buy_lots(session)

    await recompute_holding_costs_job(workspace_id=WORKSPACE_ID)

    async with postgres.async_session_maker() as session:
        result = await session.execute(
            select(Holding).where(
                Holding.workspace_id == WORKSPACE_ID,
                Holding.symbol == "MU",
            )
        )
        holding = result.scalar_one()

    # FIFO across two lots: (10*100 + 10*200) / 20 = 150.000000
    assert holding.avg_cost == Decimal("150.000000")
    assert holding.quantity == Decimal("20")


@pytest.mark.asyncio
async def test_recompute_job_is_idempotent(override_database_url):
    """Re-running the repair job must not change the already-correct basis."""
    async with postgres.async_session_maker() as session, session.begin():
        await _seed_two_buy_lots(session)

    await recompute_holding_costs_job(workspace_id=WORKSPACE_ID)
    async with postgres.async_session_maker() as session:
        first = (
            await session.execute(
                select(Holding.avg_cost).where(
                    Holding.workspace_id == WORKSPACE_ID, Holding.symbol == "MU"
                )
            )
        ).scalar_one()

    await recompute_holding_costs_job(workspace_id=WORKSPACE_ID)
    async with postgres.async_session_maker() as session:
        second = (
            await session.execute(
                select(Holding.avg_cost).where(
                    Holding.workspace_id == WORKSPACE_ID, Holding.symbol == "MU"
                )
            )
        ).scalar_one()

    assert first == second == Decimal("150.000000")


@pytest.mark.asyncio
async def test_recompute_job_requires_explicit_workspace_id():
    """Without a workspace the job must refuse rather than silently no-op."""
    with pytest.raises(ValueError, match="workspace_id"):
        await recompute_holding_costs_job(workspace_id=None)


@pytest.mark.asyncio
async def test_cli_requires_workspace_id_for_recompute_job():
    """The CLI must reject the job when --workspace-id is omitted."""
    with (
        patch("app.cli.run.JOBS", {"recompute_holding_costs": AsyncMock()}),
        patch.object(sys, "argv", ["run.py", "recompute_holding_costs"]),
        pytest.raises(SystemExit) as exc,
    ):
        await main()
    assert exc.value.code == 1


@pytest.mark.asyncio
async def test_cli_passes_workspace_id_to_recompute_job():
    """The CLI must forward --workspace-id to the job."""
    mock_job = AsyncMock()
    with (
        patch("app.cli.run.JOBS", {"recompute_holding_costs": mock_job}),
        patch.object(sys, "argv", ["run.py", "recompute_holding_costs", "--workspace-id", "9401"]),
    ):
        await main()
    mock_job.assert_called_once_with(workspace_id=9401)


def test_recompute_job_is_registered_in_jobs_dict():
    assert JOBS["recompute_holding_costs"] is recompute_holding_costs_job
