"""Concurrency tests: idempotent spending/order writes under simultaneous duplicate requests.

``source_ref`` and natural-key dedupe are check-then-insert and the schema has no
unique index on them, so two identical requests racing would both pass the check
and both insert. The write paths therefore take a transaction-scoped Postgres
advisory lock (``lock_idempotent_writes``) before checking.

Like ``tests/investing/test_holding_upsert_race.py`` these tests need genuinely
independent, committing connections, so they bypass the savepoint-per-test
fixture. Unlike it, they also write immutable audit rows (which cannot be deleted),
so each test runs in a throwaway database cloned from the migrated test database
(``CREATE DATABASE ... TEMPLATE``) and drops it afterwards; nothing leaks into the
shared test database.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.auth.models import User
from app.capture.tools import AgentTools
from app.config import settings
from app.core.database import postgres
from app.finance.models import Account, AccountType, WorkspaceFinanceSetting
from app.investing.models import CashBalance, InvestingOrder
from app.mcp.server import create_mcp_server
from app.platform.models import Workspace, WorkspaceMembership
from app.spending.models import SpendingCategory, SpendingTransaction
from app.spending.repository import TransactionRepository

pytestmark = pytest.mark.asyncio


async def _seed(engine: AsyncEngine, *, brokerage: bool = False) -> dict:
    suffix = uuid.uuid4().hex[:8]
    async with AsyncSession(engine, expire_on_commit=False) as session:
        user = User(
            email=f"race-{suffix}@example.com",
            username=f"race_{suffix}",
            hashed_password="not-a-real-hash",
        )
        workspace = Workspace(name=f"race-{suffix}")
        session.add_all([user, workspace])
        await session.flush()
        session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="owner"))
        session.add(
            SpendingCategory(workspace_id=workspace.id, name="food", normalized_name="food")
        )
        session.add(
            SpendingCategory(workspace_id=workspace.id, name="other", normalized_name="other")
        )
        wallet = Account(
            workspace_id=workspace.id,
            name="Wallet",
            default_currency_code="USD",
            account_type=AccountType.wallet,
        )
        session.add(wallet)
        broker = None
        if brokerage:
            broker = Account(
                workspace_id=workspace.id,
                name="Broker",
                default_currency_code="USD",
                account_type=AccountType.brokerage,
            )
            session.add(broker)
        await session.flush()
        session.add(
            WorkspaceFinanceSetting(
                workspace_id=workspace.id, default_spending_account_id=wallet.id
            )
        )
        if broker is not None:
            session.add(
                CashBalance(
                    workspace_id=workspace.id,
                    user_id=user.id,
                    account_id=broker.id,
                    balance=Decimal("100000.00"),
                    currency="USD",
                    as_of=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
        await session.commit()
        return {
            "user_id": user.id,
            "workspace_id": workspace.id,
            "broker_public_id": broker.public_id if broker else None,
        }


async def _count_tx(engine: AsyncEngine, ws: int) -> int:
    async with AsyncSession(engine) as session:
        return (
            await session.execute(
                select(func.count(SpendingTransaction.id)).where(
                    SpendingTransaction.workspace_id == ws
                )
            )
        ).scalar_one()


async def _count_orders(engine: AsyncEngine, ws: int) -> int:
    async with AsyncSession(engine) as session:
        return (
            await session.execute(
                select(func.count(InvestingOrder.id)).where(InvestingOrder.workspace_id == ws)
            )
        ).scalar_one()


def _item(**over):
    item = {
        "amount": "50.00",
        "category_name": "food",
        "description": "Chai",
        "account_name": "Wallet",
        "occurred_at": "2026-07-03",
    }
    item.update(over)
    return item


async def _run_tool(engine: AsyncEngine, seed: dict, method: str, **kwargs) -> dict:
    """Run an AgentTools method the way the MCP/voice runners do (commit on success)."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        tools = AgentTools(
            session,
            seed["user_id"],
            seed["workspace_id"],
            user_timezone="Asia/Kolkata",
            source_channel="mcp_agent",
        )
        result = await getattr(tools, method)(**kwargs)
        if result.get("status") == "success":
            await session.commit()
        else:
            await session.rollback()
        return result


async def test_advisory_lock_blocks_a_second_writer_until_commit(race_engine: AsyncEngine) -> None:
    """The lock really is exclusive per workspace and is released on commit."""
    probe = text("SELECT pg_try_advisory_xact_lock(:ns, :ws)")
    ns = 1_397_772_356  # TransactionRepository's lock namespace
    async with AsyncSession(race_engine) as first, AsyncSession(race_engine) as second:
        await TransactionRepository(first).lock_idempotent_writes(424242)
        assert (await second.execute(probe, {"ns": ns, "ws": 424242})).scalar_one() is False
        await second.rollback()
        # A different workspace is not blocked.
        assert (await second.execute(probe, {"ns": ns, "ws": 424243})).scalar_one() is True
        await second.rollback()
        await first.commit()
        assert (await second.execute(probe, {"ns": ns, "ws": 424242})).scalar_one() is True
        await second.rollback()


async def test_concurrent_batches_with_the_same_source_ref_create_one_row(
    race_engine: AsyncEngine,
) -> None:
    seed = await _seed(race_engine)
    batch = [_item(source_ref="race-ref-1"), _item(description="Bus", source_ref="race-ref-2")]
    results = await asyncio.gather(*[
        _run_tool(race_engine, seed, "log_spending_transactions_batch", items=batch)
        for _ in range(4)
    ])
    assert all(r["status"] == "success" for r in results), results
    assert sum(r["counts"]["create"] for r in results) == 2
    assert sum(r["counts"]["duplicate"] for r in results) == 6
    assert await _count_tx(race_engine, seed["workspace_id"]) == 2


async def test_concurrent_single_writes_with_the_same_source_ref_create_one_row(
    race_engine: AsyncEngine,
) -> None:
    seed = await _seed(race_engine)
    args = _item(source_ref="single-race")
    results = await asyncio.gather(*[
        _run_tool(race_engine, seed, "log_spending_transaction", **args) for _ in range(4)
    ])
    assert all(r["status"] == "success" for r in results), results
    assert len({r["entity_public_id"] for r in results}) == 1
    assert await _count_tx(race_engine, seed["workspace_id"]) == 1


async def test_concurrent_natural_key_batches_respect_multiplicity(
    race_engine: AsyncEngine,
) -> None:
    """Three simultaneous retries of one 2-chai batch still leave exactly 2 chais."""
    seed = await _seed(race_engine)
    results = await asyncio.gather(*[
        _run_tool(
            race_engine,
            seed,
            "log_spending_transactions_batch",
            items=[_item(), _item()],
            skip_duplicates=True,
        )
        for _ in range(3)
    ])
    assert all(r["status"] == "success" for r in results), results
    assert sum(r["counts"]["create"] for r in results) == 2
    assert await _count_tx(race_engine, seed["workspace_id"]) == 2


async def test_batches_in_different_workspaces_do_not_block_each_other(
    race_engine: AsyncEngine,
) -> None:
    first = await _seed(race_engine)
    second = await _seed(race_engine)
    async with AsyncSession(race_engine) as holder:
        # Hold workspace A's lock open in an uncommitted transaction...
        await TransactionRepository(holder).lock_idempotent_writes(first["workspace_id"])
        # ...workspace B must still complete promptly.
        result = await asyncio.wait_for(
            _run_tool(
                race_engine,
                second,
                "log_spending_transactions_batch",
                items=[_item(source_ref="other-ws")],
            ),
            timeout=10,
        )
        assert result["counts"]["create"] == 1
        await holder.rollback()


async def _call_order_tool(monkeypatch, engine, seed, **kwargs) -> dict:
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def authorize(*_a, **_k):
        return seed["user_id"]

    monkeypatch.setattr(postgres, "async_session_maker", maker)
    monkeypatch.setattr("app.mcp.security.authorize_workspace", authorize)
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.lifestack.test")
    tools = {t.name: t.fn for t in await create_mcp_server().list_tools()}
    return await tools["create_investment_order"](
        workspace_id=seed["workspace_id"],
        account_id=str(seed["broker_public_id"]),
        order_type="buy",
        symbol="AAPL",
        quantity="2",
        price_per_unit="100",
        currency="USD",
        occurred_at="2026-07-03T10:00:00+00:00",
        confirmed=True,
        **kwargs,
    )


async def test_concurrent_orders_with_the_same_source_ref_create_one_order(
    race_engine: AsyncEngine, monkeypatch
) -> None:
    seed = await _seed(race_engine, brokerage=True)
    results = await asyncio.gather(*[
        _call_order_tool(monkeypatch, race_engine, seed, source_ref="ord-race") for _ in range(3)
    ])
    assert all(r["status"] == "success" for r in results), results
    assert sum(1 for r in results if r.get("idempotent")) == 2
    assert len({r["entity_public_id"] for r in results}) == 1
    assert await _count_orders(race_engine, seed["workspace_id"]) == 1


async def test_concurrent_identical_orders_without_source_ref_record_only_one(
    race_engine: AsyncEngine, monkeypatch
) -> None:
    seed = await _seed(race_engine, brokerage=True)
    results = await asyncio.gather(*[
        _call_order_tool(monkeypatch, race_engine, seed) for _ in range(3)
    ])
    assert sorted(r["status"] for r in results) == [
        "possible_duplicate",
        "possible_duplicate",
        "success",
    ], results
    assert await _count_orders(race_engine, seed["workspace_id"]) == 1
