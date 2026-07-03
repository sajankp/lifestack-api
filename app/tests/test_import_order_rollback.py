"""Unit tests for investing-orders import rollback orchestration.

Rolling back an orders import must not just delete the order rows: placing the
orders created cash-balance snapshots and mutated holdings, so the rollback also
removes the order-triggered cash balances and recomputes affected holdings.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import ValidationError
from app.imports.repository import ImportRepository
from app.imports.service import ImportService
from app.investing.models import InvestingOrder

WS = 1
USER = 7
BATCH_ID = 99


def _order(symbol: str, account_id: int) -> InvestingOrder:
    return InvestingOrder(
        public_id=uuid.uuid4(),
        workspace_id=WS,
        user_id=USER,
        account_id=account_id,
        order_type="buy",
        symbol=symbol,
        quantity=Decimal("10"),
        price_per_unit=Decimal("100"),
        gross_amount=Decimal("1000"),
        net_amount=Decimal("1000"),
        currency="USD",
        occurred_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_rollback_orders_clears_cash_balances_and_recomputes_holdings():
    orders = [_order("AAPL", 10), _order("AAPL", 10), _order("MSFT", 11)]

    repo = AsyncMock()
    repo.list_investing_orders_for_batch = AsyncMock(return_value=orders)
    repo.delete_cash_balances_by_trigger_refs = AsyncMock(return_value=3)
    repo.delete_investing_orders_for_batch = AsyncMock(return_value=3)

    order_service = AsyncMock()
    svc = ImportService(repo, AsyncMock(), order_service=order_service)

    deleted = await svc._rollback_investing_orders(WS, USER, BATCH_ID)

    assert deleted == 3
    # Cash balances removed by the rolled-back orders' public ids.
    refs = repo.delete_cash_balances_by_trigger_refs.call_args.args[2]
    assert sorted(refs) == sorted(o.public_id for o in orders)
    assert repo.delete_cash_balances_by_trigger_refs.call_args.args[1] == "order"
    # Holdings recomputed once per affected (symbol, account) pair.
    recomputed = {(c.args[2], c.args[3]) for c in order_service._recompute_holding.call_args_list}
    assert recomputed == {("AAPL", 10), ("MSFT", 11)}


@pytest.mark.asyncio
async def test_rollback_orders_no_orders_is_noop():
    repo = AsyncMock()
    repo.list_investing_orders_for_batch = AsyncMock(return_value=[])
    order_service = AsyncMock()
    svc = ImportService(repo, AsyncMock(), order_service=order_service)

    assert await svc._rollback_investing_orders(WS, USER, BATCH_ID) == 0
    repo.delete_cash_balances_by_trigger_refs.assert_not_called()
    repo.delete_investing_orders_for_batch.assert_not_called()
    order_service._recompute_holding.assert_not_called()


@pytest.mark.asyncio
async def test_rollback_orders_requires_order_service():
    repo = AsyncMock()
    svc = ImportService(repo, AsyncMock(), order_service=None)

    with pytest.raises(ValidationError):
        await svc._rollback_investing_orders(WS, USER, BATCH_ID)


@pytest.mark.parametrize(
    ("workspace_id", "import_batch_id"),
    [(None, BATCH_ID), (WS, None), (None, None)],
)
@pytest.mark.asyncio
async def test_batch_deleters_fail_closed_on_missing_ids(workspace_id, import_batch_id):
    """Missing identifiers must raise before any SQL runs (no IS NULL bulk delete)."""
    session = AsyncMock()
    repo = ImportRepository(session)

    with pytest.raises(ValueError):
        await repo.delete_investing_orders_for_batch(workspace_id, import_batch_id)
    with pytest.raises(ValueError):
        await repo.list_investing_orders_for_batch(workspace_id, import_batch_id)

    session.execute.assert_not_called()
    session.flush.assert_not_called()


@pytest.mark.asyncio
async def test_delete_cash_balances_fail_closed_on_missing_workspace():
    session = AsyncMock()
    repo = ImportRepository(session)

    with pytest.raises(ValueError):
        await repo.delete_cash_balances_by_trigger_refs(None, "order", [uuid.uuid4()])

    session.execute.assert_not_called()


def test_import_service_clears_instance_cache_on_session_change():
    """Regression test for PR #81 thread PRRT_kwDORhelTM6M3Zvi: the in-memory
    per-instance cash-balance cache must be cleared whenever the service's
    session identity changes, so stale cache entries built against a closed
    or replaced session can never leak into work done under a new session."""
    repo = AsyncMock()
    session_a = AsyncMock()
    svc = ImportService(repo, session_a, order_service=None)

    svc._cash_balance_cache[(1, "USD")] = Decimal("500")
    svc._ensure_cache_session()
    # Same session -- cache untouched.
    assert svc._cash_balance_cache == {(1, "USD"): Decimal("500")}

    session_b = AsyncMock()
    svc.session = session_b
    svc._ensure_cache_session()
    # Session identity changed -- cache must be cleared.
    assert svc._cash_balance_cache == {}
    assert svc._cache_session is session_b
