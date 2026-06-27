"""Unit tests for InvestingOrderService — repositories are mocked."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import NotFoundError, ValidationError
from app.finance.models import Account
from app.investing.models import Holding, InvestingOrder, OrderType
from app.investing.schemas import InvestingOrderCreate
from app.investing.service import InvestingOrderService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WS = 1
USER = 1
ACCOUNT_ID = 42
ACCOUNT_PUB = uuid.uuid4()


def _make_account(account_type: str = "brokerage") -> Account:
    return Account(
        id=ACCOUNT_ID,
        public_id=ACCOUNT_PUB,
        workspace_id=WS,
        name="My Brokerage",
        account_type=account_type,
        default_currency_code="USD",
        is_active=True,
    )


def _make_holding(symbol: str = "AAPL", qty: str = "10", avg_cost: str = "150.00") -> Holding:
    return Holding(
        id=1,
        public_id=uuid.uuid4(),
        workspace_id=WS,
        user_id=USER,
        symbol=symbol,
        account_id=ACCOUNT_ID,
        quantity=Decimal(qty),
        avg_cost=Decimal(avg_cost),
        currency="USD",
        source_type="order",
    )


def _make_order_create(
    order_type: OrderType = OrderType.buy,
    symbol: str = "AAPL",
    quantity: str = "10",
    price: str = "150.00",
    fee: str = "0",
) -> InvestingOrderCreate:
    return InvestingOrderCreate(
        account_id=ACCOUNT_PUB,
        order_type=order_type,
        symbol=symbol,
        quantity=Decimal(quantity),
        price_per_unit=Decimal(price),
        currency="USD",
        brokerage_fee=Decimal(fee),
        occurred_at=datetime.now(UTC),
    )


def _make_service(
    *,
    account: Account | None = None,
    existing_holding: Holding | None = None,
    cash_balance: Decimal = Decimal("10000"),
    saved_order: InvestingOrder | None = None,
) -> InvestingOrderService:
    account = account or _make_account()

    order_repo = AsyncMock()
    holding_repo = AsyncMock()
    cash_balance_repo = AsyncMock()
    account_repo = AsyncMock()
    currency_repo = AsyncMock()
    instrument_service = AsyncMock()

    account_repo.get_by_public_id = AsyncMock(return_value=account)
    holding_repo.get_by_unique_key = AsyncMock(return_value=existing_holding)

    latest_cash = MagicMock()
    latest_cash.balance = cash_balance
    cash_balance_repo.get_latest_for_account_currency = AsyncMock(return_value=latest_cash)
    cash_balance_repo.create = AsyncMock(side_effect=lambda cb: cb)

    instrument_service.find_or_create_instrument = AsyncMock(return_value=None)

    def _create_order_side_effect(order: InvestingOrder) -> InvestingOrder:
        order.id = 99
        order.public_id = saved_order.public_id if saved_order else uuid.uuid4()
        return order

    order_repo.create = AsyncMock(side_effect=_create_order_side_effect)
    holding_repo.create = AsyncMock(side_effect=lambda h: h)
    holding_repo.save = AsyncMock(side_effect=lambda h: h)

    return InvestingOrderService(
        order_repository=order_repo,
        holding_repository=holding_repo,
        cash_balance_repository=cash_balance_repo,
        account_repository=account_repo,
        currency_repository=currency_repo,
        instrument_service=instrument_service,
    )


# ---------------------------------------------------------------------------
# avg_cost computation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_buy_creates_holding_with_correct_avg_cost():
    svc = _make_service(existing_holding=None, cash_balance=Decimal("10000"))
    order_in = _make_order_create(
        order_type=OrderType.buy, symbol="AAPL", quantity="10", price="150.00"
    )

    await svc.place_order(WS, USER, order_in)

    created_holding = svc.holding_repository.create.call_args[0][0]
    assert created_holding.quantity == Decimal("10")
    assert created_holding.avg_cost == Decimal("150.000000")


@pytest.mark.asyncio
async def test_second_buy_computes_weighted_avg_cost():
    existing = _make_holding(qty="10", avg_cost="150.00")
    svc = _make_service(existing_holding=existing, cash_balance=Decimal("10000"))
    order_in = _make_order_create(
        order_type=OrderType.buy, symbol="AAPL", quantity="5", price="170.00"
    )

    await svc.place_order(WS, USER, order_in)

    saved = svc.holding_repository.save.call_args[0][0]
    assert saved.quantity == Decimal("15")
    expected_avg = (
        (Decimal("10") * Decimal("150") + Decimal("5") * Decimal("170")) / Decimal("15")
    ).quantize(Decimal("0.000001"))
    assert saved.avg_cost == expected_avg


@pytest.mark.asyncio
async def test_sell_reduces_quantity_keeps_avg_cost():
    existing = _make_holding(qty="10", avg_cost="150.00")
    svc = _make_service(existing_holding=existing, cash_balance=Decimal("0"))
    order_in = _make_order_create(
        order_type=OrderType.sell, symbol="AAPL", quantity="3", price="180.00"
    )

    await svc.place_order(WS, USER, order_in)

    saved = svc.holding_repository.save.call_args[0][0]
    assert saved.quantity == Decimal("7")
    assert saved.avg_cost == Decimal("150.00")


@pytest.mark.asyncio
async def test_sell_records_realized_gain_loss():
    existing = _make_holding(qty="10", avg_cost="150.00")
    svc = _make_service(existing_holding=existing)
    order_in = _make_order_create(
        order_type=OrderType.sell, symbol="AAPL", quantity="3", price="180.00"
    )

    created_order = await svc.place_order(WS, USER, order_in)

    # realized G/L = 3 × (180 - 150) = 90
    assert created_order.realized_gain_loss == Decimal("90.00")
    assert created_order.avg_cost_at_sale == Decimal("150.00")


@pytest.mark.asyncio
async def test_sell_all_shares_sets_quantity_to_zero():
    existing = _make_holding(qty="10", avg_cost="150.00")
    svc = _make_service(existing_holding=existing)
    order_in = _make_order_create(
        order_type=OrderType.sell, symbol="AAPL", quantity="10", price="180.00"
    )

    await svc.place_order(WS, USER, order_in)

    saved = svc.holding_repository.save.call_args[0][0]
    assert saved.quantity == Decimal("0")
    assert saved.avg_cost == Decimal("150.00")


@pytest.mark.asyncio
async def test_buy_after_selling_all_resets_avg_cost():
    # qty=0 means "fresh start" for avg_cost
    existing = _make_holding(qty="0", avg_cost="150.00")
    svc = _make_service(existing_holding=existing, cash_balance=Decimal("10000"))
    order_in = _make_order_create(
        order_type=OrderType.buy, symbol="AAPL", quantity="5", price="200.00"
    )

    await svc.place_order(WS, USER, order_in)

    saved = svc.holding_repository.save.call_args[0][0]
    assert saved.quantity == Decimal("5")
    # new avg cost should be just the new price (0 * old_avg + 5 * 200) / 5 = 200
    assert saved.avg_cost == Decimal("200.000000")


# ---------------------------------------------------------------------------
# Cash balance updates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_deducts_net_amount_from_cash():
    svc = _make_service(existing_holding=None, cash_balance=Decimal("5000"))
    order_in = _make_order_create(
        order_type=OrderType.buy, quantity="10", price="150.00", fee="1.99"
    )

    await svc.place_order(WS, USER, order_in)

    created_cash = svc.cash_balance_repository.create.call_args[0][0]
    expected_net = Decimal("10") * Decimal("150") + Decimal("1.99")  # 1501.99
    assert created_cash.balance == Decimal("5000") - expected_net
    assert created_cash.trigger_type == "order"


@pytest.mark.asyncio
async def test_sell_adds_net_amount_to_cash():
    existing = _make_holding(qty="10", avg_cost="150.00")
    svc = _make_service(existing_holding=existing, cash_balance=Decimal("0"))
    order_in = _make_order_create(
        order_type=OrderType.sell, quantity="5", price="180.00", fee="1.99"
    )

    await svc.place_order(WS, USER, order_in)

    created_cash = svc.cash_balance_repository.create.call_args[0][0]
    expected_net = Decimal("5") * Decimal("180") - Decimal("1.99")  # 898.01
    assert created_cash.balance == Decimal("0") + expected_net


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_with_insufficient_cash_raises():
    svc = _make_service(existing_holding=None, cash_balance=Decimal("100"))
    order_in = _make_order_create(order_type=OrderType.buy, quantity="10", price="150.00")

    with pytest.raises(ValidationError) as exc_info:
        await svc.place_order(WS, USER, order_in)
    assert "Insufficient cash balance" in exc_info.value.detail


@pytest.mark.asyncio
async def test_sell_more_than_owned_raises():
    existing = _make_holding(qty="5", avg_cost="150.00")
    svc = _make_service(existing_holding=existing)
    order_in = _make_order_create(order_type=OrderType.sell, quantity="10", price="180.00")

    with pytest.raises(ValidationError) as exc_info:
        await svc.place_order(WS, USER, order_in)
    assert "Cannot sell 10" in exc_info.value.detail


@pytest.mark.asyncio
async def test_sell_with_no_holding_raises():
    svc = _make_service(existing_holding=None)
    order_in = _make_order_create(order_type=OrderType.sell, quantity="5", price="180.00")

    with pytest.raises(ValidationError) as exc_info:
        await svc.place_order(WS, USER, order_in)
    assert "Cannot sell" in exc_info.value.detail


@pytest.mark.asyncio
async def test_non_brokerage_account_raises():
    bank_account = _make_account(account_type="bank")
    svc = _make_service(account=bank_account)
    order_in = _make_order_create()

    with pytest.raises(ValidationError) as exc_info:
        await svc.place_order(WS, USER, order_in)
    assert "brokerage accounts" in exc_info.value.detail


@pytest.mark.asyncio
async def test_account_not_found_raises():
    svc = _make_service()
    svc.account_repository.get_by_public_id = AsyncMock(return_value=None)
    order_in = _make_order_create()

    with pytest.raises(NotFoundError):
        await svc.place_order(WS, USER, order_in)


# ---------------------------------------------------------------------------
# net_amount computation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_net_amount_is_gross_plus_fees():
    svc = _make_service(existing_holding=None, cash_balance=Decimal("10000"))
    order_in = InvestingOrderCreate(
        account_id=ACCOUNT_PUB,
        order_type=OrderType.buy,
        symbol="AAPL",
        quantity=Decimal("10"),
        price_per_unit=Decimal("150.00"),
        currency="USD",
        brokerage_fee=Decimal("1.99"),
        tax_amount=Decimal("0.50"),
        other_fees=Decimal("0.25"),
        occurred_at=datetime.now(UTC),
    )

    created = await svc.place_order(WS, USER, order_in)

    assert created.gross_amount == Decimal("1500.00")
    assert created.net_amount == Decimal("1502.74")  # 1500 + 1.99 + 0.50 + 0.25


@pytest.mark.asyncio
async def test_sell_net_amount_is_gross_minus_fees():
    existing = _make_holding(qty="10", avg_cost="150.00")
    svc = _make_service(existing_holding=existing)
    order_in = InvestingOrderCreate(
        account_id=ACCOUNT_PUB,
        order_type=OrderType.sell,
        symbol="AAPL",
        quantity=Decimal("5"),
        price_per_unit=Decimal("180.00"),
        currency="USD",
        brokerage_fee=Decimal("1.99"),
        tax_amount=Decimal("0.50"),
        other_fees=Decimal("0.25"),
        occurred_at=datetime.now(UTC),
    )

    created = await svc.place_order(WS, USER, order_in)

    assert created.gross_amount == Decimal("900.00")
    assert created.net_amount == Decimal("897.26")  # 900 - 1.99 - 0.50 - 0.25


# ---------------------------------------------------------------------------
# Delete order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_order_not_found_raises():
    svc = _make_service()
    svc.order_repository.get_by_public_id = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError) as exc_info:
        await svc.delete_order(WS, USER, uuid.uuid4())
    assert "not found" in exc_info.value.detail


@pytest.mark.asyncio
async def test_delete_buy_order_that_would_leave_negative_holding_raises():
    order = InvestingOrder(
        id=1,
        public_id=uuid.uuid4(),
        workspace_id=WS,
        user_id=USER,
        account_id=ACCOUNT_ID,
        order_type="buy",
        symbol="AAPL",
        quantity=Decimal("5"),
        price_per_unit=Decimal("150"),
        gross_amount=Decimal("750"),
        net_amount=Decimal("750"),
        currency="USD",
        occurred_at=datetime.now(UTC),
    )
    svc = _make_service()
    svc.order_repository.get_by_public_id = AsyncMock(return_value=order)
    # Aggregated: 10 bought, 8 sold, deleting 5 of the buys leaves 5 buys vs 8 sells
    svc.order_repository.sum_by_symbol_account = AsyncMock(
        return_value={
            "total_buy_qty": Decimal("10"),
            "total_sell_qty": Decimal("8"),
            "total_cost_basis": Decimal("1500"),
            "net_qty": Decimal("2"),
        }
    )

    with pytest.raises(ValidationError) as exc_info:
        await svc.delete_order(WS, USER, order.public_id)
    assert "negative holding" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Bulk import ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_import_processes_orders_chronologically():
    svc = _make_service(existing_holding=None, cash_balance=Decimal("100000"))

    early = _make_order_create(order_type=OrderType.buy, quantity="10", price="100.00")
    late = _make_order_create(order_type=OrderType.buy, quantity="5", price="200.00")
    late.occurred_at = datetime(2026, 6, 27, tzinfo=UTC)
    early.occurred_at = datetime(2026, 1, 1, tzinfo=UTC)

    call_order: list[str] = []

    async def _fake_place(workspace_id, user_id, order_in, **kwargs):
        call_order.append(order_in.occurred_at.isoformat())
        order = InvestingOrder(
            id=len(call_order),
            public_id=uuid.uuid4(),
            workspace_id=workspace_id,
            user_id=user_id,
            account_id=ACCOUNT_ID,
            order_type=order_in.order_type.value,
            symbol=order_in.symbol,
            quantity=order_in.quantity,
            price_per_unit=order_in.price_per_unit,
            gross_amount=order_in.quantity * order_in.price_per_unit,
            net_amount=order_in.quantity * order_in.price_per_unit,
            currency=order_in.currency,
            occurred_at=order_in.occurred_at,
        )
        return order

    svc.place_order = _fake_place  # type: ignore[method-assign]

    await svc.bulk_import_orders(WS, USER, [late, early], source_import_id=None)

    assert call_order[0] < call_order[1], "Orders should be processed earliest first"
