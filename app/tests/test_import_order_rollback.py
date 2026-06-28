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
    recomputed = {
        (c.args[2], c.args[3]) for c in order_service._recompute_holding_from_orders.call_args_list
    }
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
    order_service._recompute_holding_from_orders.assert_not_called()


@pytest.mark.asyncio
async def test_rollback_orders_requires_order_service():
    repo = AsyncMock()
    svc = ImportService(repo, AsyncMock(), order_service=None)

    with pytest.raises(ValidationError):
        await svc._rollback_investing_orders(WS, USER, BATCH_ID)
