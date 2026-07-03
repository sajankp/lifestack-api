"""Unit tests for InvestingOrderService — repositories are mocked."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.audit import AuditLogger
from app.core.exceptions import NotFoundError, ValidationError
from app.finance.models import Account
from app.investing.models import Holding, InvestingOrder, OrderType
from app.investing.order_service import InvestingOrderService
from app.investing.schemas import InvestingOrderCreate, InvestingOrderUpdate

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


def _make_order(
    *,
    id: int,
    order_type: str,
    quantity: str,
    price: str,
    occurred_at: datetime,
    public_id: uuid.UUID | None = None,
) -> InvestingOrder:
    qty = Decimal(quantity)
    unit = Decimal(price)
    return InvestingOrder(
        id=id,
        public_id=public_id or uuid.uuid4(),
        workspace_id=WS,
        user_id=USER,
        account_id=ACCOUNT_ID,
        order_type=order_type,
        symbol="AAPL",
        quantity=qty,
        price_per_unit=unit,
        gross_amount=qty * unit,
        net_amount=qty * unit,
        currency="USD",
        occurred_at=occurred_at,
    )


def _make_service(
    *,
    account: Account | None = None,
    existing_holding: Holding | None = None,
    existing_orders: list[InvestingOrder] | None = None,
    cash_balance: Decimal = Decimal("10000"),
) -> InvestingOrderService:
    """Build an InvestingOrderService backed by an in-memory fake order store.

    place_order/update_order/delete_order all route through
    ``_recompute_holding``, which replays the *full* order
    history (FIFO) on every write. A static AsyncMock holding stub can't
    drive that — the mocks need a shared, mutable store so `create`/`save`/
    `list_by_holding`/`get_by_public_id` all see the same orders.
    """
    account = account or _make_account()

    order_repo = AsyncMock()
    holding_repo = AsyncMock()
    cash_balance_repo = AsyncMock()
    account_repo = AsyncMock()
    currency_repo = AsyncMock()
    instrument_service = AsyncMock()
    lot_repo = AsyncMock()
    corporate_action_repo = AsyncMock()
    corporate_action_repo.list_by_holding = AsyncMock(return_value=[])

    account_repo.get_by_public_id = AsyncMock(return_value=account)
    holding_repo.get_by_unique_key = AsyncMock(return_value=existing_holding)

    latest_cash = MagicMock()
    latest_cash.balance = cash_balance
    cash_balance_repo.get_latest_for_account_currency = AsyncMock(return_value=latest_cash)
    cash_balance_repo.create = AsyncMock(side_effect=lambda cb: cb)

    instrument_service.find_or_create_instrument = AsyncMock(return_value=None)

    store: dict[int, InvestingOrder] = {o.id: o for o in (existing_orders or []) if o.id}
    next_id = [max(store.keys(), default=0) + 1]

    async def _create_order(order: InvestingOrder) -> InvestingOrder:
        order.id = next_id[0]
        next_id[0] += 1
        store[order.id] = order
        return order

    async def _save_order(order: InvestingOrder) -> InvestingOrder:
        assert order.id is not None
        store[order.id] = order
        return order

    async def _get_order_by_public_id(
        workspace_id: int, public_id: uuid.UUID
    ) -> InvestingOrder | None:
        return next((o for o in store.values() if o.public_id == public_id), None)

    async def _list_orders_by_holding(
        workspace_id: int, symbol: str, account_id: int
    ) -> list[InvestingOrder]:
        rows = [o for o in store.values() if o.symbol == symbol and o.account_id == account_id]
        return sorted(rows, key=lambda o: (o.occurred_at, o.id))

    async def _delete_order(order: InvestingOrder) -> None:
        store.pop(order.id, None)

    order_repo.create = AsyncMock(side_effect=_create_order)
    order_repo.save = AsyncMock(side_effect=_save_order)
    order_repo.get_by_public_id = AsyncMock(side_effect=_get_order_by_public_id)
    order_repo.list_by_holding = AsyncMock(side_effect=_list_orders_by_holding)
    order_repo.delete = AsyncMock(side_effect=_delete_order)

    def _create_holding(h: Holding) -> Holding:
        if h.id is None:
            h.id = 1
        return h

    holding_repo.create = AsyncMock(side_effect=_create_holding)
    holding_repo.save = AsyncMock(side_effect=lambda h: h)

    lot_next_id = [1]

    async def _create_lots(lots: list) -> list:
        for lot in lots:
            lot.id = lot_next_id[0]
            lot_next_id[0] += 1
        return lots

    lot_repo.create_lots = AsyncMock(side_effect=_create_lots)
    lot_repo.create_consumptions = AsyncMock(side_effect=lambda cs: cs)
    lot_repo.delete_for_holding = AsyncMock(return_value=None)

    return InvestingOrderService(
        order_repository=order_repo,
        holding_repository=holding_repo,
        cash_balance_repository=cash_balance_repo,
        account_repository=account_repo,
        currency_repository=currency_repo,
        instrument_service=instrument_service,
        lot_repository=lot_repo,
        corporate_action_repository=corporate_action_repo,
    )


# ---------------------------------------------------------------------------
# avg_cost computation (FIFO — spec-044)
# ---------------------------------------------------------------------------

EARLY = datetime(2026, 1, 1, tzinfo=UTC)


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
async def test_second_buy_computes_weighted_avg_cost_across_open_lots():
    prior_buy = _make_order(id=1, order_type="buy", quantity="10", price="150", occurred_at=EARLY)
    existing = _make_holding(qty="10", avg_cost="150.00")
    svc = _make_service(
        existing_holding=existing, existing_orders=[prior_buy], cash_balance=Decimal("10000")
    )
    order_in = _make_order_create(
        order_type=OrderType.buy, symbol="AAPL", quantity="5", price="170.00"
    )

    await svc.place_order(WS, USER, order_in)

    saved = svc.holding_repository.save.call_args[0][0]
    assert saved.quantity == Decimal("15")
    # No sells have happened, so FIFO's "average of open lots" equals the
    # same weighted average as before across the two still-open lots.
    expected_avg = (
        (Decimal("10") * Decimal("150") + Decimal("5") * Decimal("170")) / Decimal("15")
    ).quantize(Decimal("0.000001"))
    assert saved.avg_cost == expected_avg


@pytest.mark.asyncio
async def test_sell_reduces_quantity_keeps_avg_cost_of_remaining_lot():
    prior_buy = _make_order(id=1, order_type="buy", quantity="10", price="150", occurred_at=EARLY)
    existing = _make_holding(qty="10", avg_cost="150.00")
    svc = _make_service(
        existing_holding=existing, existing_orders=[prior_buy], cash_balance=Decimal("0")
    )
    order_in = _make_order_create(
        order_type=OrderType.sell, symbol="AAPL", quantity="3", price="180.00"
    )

    await svc.place_order(WS, USER, order_in)

    saved = svc.holding_repository.save.call_args[0][0]
    assert saved.quantity == Decimal("7")
    # Single lot, partially consumed: the remaining 7 units are still costed
    # at that lot's original price (150), same as the old moving-average
    # behavior in the single-lot case.
    assert saved.avg_cost == Decimal("150.00")


@pytest.mark.asyncio
async def test_sell_records_realized_gain_loss_against_fifo_lot():
    prior_buy = _make_order(id=1, order_type="buy", quantity="10", price="150", occurred_at=EARLY)
    existing = _make_holding(qty="10", avg_cost="150.00")
    svc = _make_service(existing_holding=existing, existing_orders=[prior_buy])
    order_in = _make_order_create(
        order_type=OrderType.sell, symbol="AAPL", quantity="3", price="180.00"
    )

    created_order = await svc.place_order(WS, USER, order_in)

    # realized G/L = 3 × (180 - 150) = 90, costed against the one open lot
    assert created_order.realized_gain_loss == Decimal("90.00")
    assert created_order.avg_cost_at_sale == Decimal("150.00")


@pytest.mark.asyncio
async def test_sell_all_shares_sets_quantity_and_avg_cost_to_zero():
    prior_buy = _make_order(id=1, order_type="buy", quantity="10", price="150", occurred_at=EARLY)
    existing = _make_holding(qty="10", avg_cost="150.00")
    svc = _make_service(existing_holding=existing, existing_orders=[prior_buy])
    order_in = _make_order_create(
        order_type=OrderType.sell, symbol="AAPL", quantity="10", price="180.00"
    )

    await svc.place_order(WS, USER, order_in)

    saved = svc.holding_repository.save.call_args[0][0]
    assert saved.quantity == Decimal("0")
    # No open lots remain once fully sold, so avg_cost resets to 0. This is
    # a deliberate behavior change from the old place_order-only inline
    # logic (which never touched avg_cost on sell): every write path now
    # goes through the same full FIFO replay, so a fully-closed position
    # has no cost basis left to report — consistent with delete/update_order's
    # pre-existing behavior.
    assert saved.avg_cost == Decimal("0")


@pytest.mark.asyncio
async def test_buy_after_selling_all_starts_a_fresh_lot():
    prior_buy = _make_order(id=1, order_type="buy", quantity="10", price="150", occurred_at=EARLY)
    prior_sell = _make_order(
        id=2,
        order_type="sell",
        quantity="10",
        price="180",
        occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    existing = _make_holding(qty="0", avg_cost="0")
    svc = _make_service(
        existing_holding=existing,
        existing_orders=[prior_buy, prior_sell],
        cash_balance=Decimal("10000"),
    )
    order_in = _make_order_create(
        order_type=OrderType.buy, symbol="AAPL", quantity="5", price="200.00"
    )

    await svc.place_order(WS, USER, order_in)

    saved = svc.holding_repository.save.call_args[0][0]
    assert saved.quantity == Decimal("5")
    # No open lots remained, so this buy starts a fresh lot at its own price.
    assert saved.avg_cost == Decimal("200.000000")


# ---------------------------------------------------------------------------
# Fee capitalization and book-value precision (spec-046)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_capitalizes_brokerage_into_avg_cost():
    svc = _make_service(existing_holding=None, cash_balance=Decimal("10000"))
    order_in = _make_order_create(
        order_type=OrderType.buy, symbol="AAPL", quantity="10", price="150.00", fee="5"
    )

    await svc.place_order(WS, USER, order_in)

    created = svc.holding_repository.create.call_args[0][0]
    # cost basis = (10 × 150 + 5 brokerage) / 10 = 150.50, not the raw 150.
    assert created.avg_cost == Decimal("150.500000")
    # book value the router will report = qty × avg_cost = net_amount paid.
    assert created.quantity * created.avg_cost == Decimal("1505.000000")


@pytest.mark.asyncio
async def test_buy_capitalizes_all_fee_types():
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

    await svc.place_order(WS, USER, order_in)

    created = svc.holding_repository.create.call_args[0][0]
    # (150 + (1.99 + 0.50 + 0.25)/10) = 150.274
    assert created.avg_cost == Decimal("150.274000")


@pytest.mark.asyncio
async def test_sell_nets_fees_from_realized_gain():
    prior_buy = _make_order(id=1, order_type="buy", quantity="10", price="150", occurred_at=EARLY)
    existing = _make_holding(qty="10", avg_cost="150.00")
    svc = _make_service(existing_holding=existing, existing_orders=[prior_buy])
    order_in = _make_order_create(
        order_type=OrderType.sell, symbol="AAPL", quantity="3", price="180.00", fee="2"
    )

    created = await svc.place_order(WS, USER, order_in)

    # gross realized = 3 × (180 - 150) = 90; sell brokerage of 2 reduces it.
    assert created.realized_gain_loss == Decimal("88.00")
    # avg_cost_at_sale is the (fee-inclusive) buy cost of consumed units — the
    # prior buy had no fees, so 150 — sell-side fees do not raise it.
    assert created.avg_cost_at_sale == Decimal("150.000000")


@pytest.mark.asyncio
async def test_buy_preserves_sub_two_decimal_cost_precision():
    # Low-NAV / high-qty mutual fund: 2-dp avg_cost would round 9.0758 → 9.08
    # and inflate book value. Cost basis must keep full precision.
    svc = _make_service(existing_holding=None, cash_balance=Decimal("100000"))
    order_in = _make_order_create(
        order_type=OrderType.buy, symbol="MF", quantity="8924.397", price="9.0758"
    )

    await svc.place_order(WS, USER, order_in)

    created = svc.holding_repository.create.call_args[0][0]
    assert created.avg_cost == Decimal("9.075800")
    # book value tracks the precise NAV (≈80,996), not 8924.397 × 9.08 = 81,033.52.
    assert created.quantity * created.avg_cost == Decimal("80996.0422926")


# ---------------------------------------------------------------------------
# FIFO lot persistence (OrderLot / LotConsumption) — spec-044
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_spanning_two_lots_records_lot_and_consumptions():
    # Mirrors the production Bandhan ELSS case from spec-044: three buys,
    # then a sell that exactly exhausts the first two lots and leaves the
    # third lot untouched.
    buy1 = _make_order(
        id=1, order_type="buy", quantity="180.573", price="110.75", occurred_at=EARLY
    )
    buy2 = _make_order(
        id=2,
        order_type="buy",
        quantity="119.528",
        price="108.76",
        occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    buy3 = _make_order(
        id=3,
        order_type="buy",
        quantity="140.319",
        price="142.53",
        occurred_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    existing = _make_holding(qty="440.420", avg_cost="120.335")
    svc = _make_service(existing_holding=existing, existing_orders=[buy1, buy2, buy3])
    order_in = _make_order_create(
        order_type=OrderType.sell, symbol="AAPL", quantity="300.101", price="182.30"
    )

    await svc.place_order(WS, USER, order_in)

    # delete_for_holding is called once for the existing holding before replay.
    svc.lot_repository.delete_for_holding.assert_called_once_with(existing.id)

    created_lots = svc.lot_repository.create_lots.call_args[0][0]
    assert {lot.buy_order_id: lot.remaining_quantity for lot in created_lots} == {
        1: Decimal("0"),
        2: Decimal("0"),
        3: Decimal("140.319"),
    }

    created_consumptions = svc.lot_repository.create_consumptions.call_args[0][0]
    # The sell drew fully from lot 1 and lot 2, never touching lot 3.
    consumed_by_buy_order = {c.lot_id: c.quantity_consumed for c in created_consumptions}
    lot_id_by_buy_order = {lot.buy_order_id: lot.id for lot in created_lots}
    assert consumed_by_buy_order[lot_id_by_buy_order[1]] == Decimal("180.573")
    assert consumed_by_buy_order[lot_id_by_buy_order[2]] == Decimal("119.528")
    assert lot_id_by_buy_order[3] not in consumed_by_buy_order

    saved_holding = svc.holding_repository.save.call_args[0][0]
    assert saved_holding.quantity == Decimal("140.319")
    # Open-lot avg cost is just lot 3's own price, matching what brokers
    # like Groww show for the remaining FIFO lot (see spec-044).
    assert saved_holding.avg_cost == Decimal("142.530000")


@pytest.mark.asyncio
async def test_backdated_sell_exceeding_chronological_capacity_raises():
    # Aggregate holding quantity (10) is enough, but placing a sell of 8
    # dated *before* the second buy would go negative at that point in the
    # chronological replay — must be rejected even though place_order's own
    # cheap pre-check only looks at the current aggregate.
    buy1 = _make_order(id=1, order_type="buy", quantity="5", price="100", occurred_at=EARLY)
    buy2 = _make_order(
        id=2,
        order_type="buy",
        quantity="5",
        price="110",
        occurred_at=datetime(2026, 1, 10, tzinfo=UTC),
    )
    existing = _make_holding(qty="10", avg_cost="105.00")
    svc = _make_service(existing_holding=existing, existing_orders=[buy1, buy2])
    order_in = _make_order_create(
        order_type=OrderType.sell, symbol="AAPL", quantity="8", price="120.00"
    )
    order_in.occurred_at = datetime(2026, 1, 5, tzinfo=UTC)  # between buy1 and buy2

    with pytest.raises(ValidationError) as exc_info:
        await svc.place_order(WS, USER, order_in)
    assert "negative holding" in exc_info.value.detail


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
    prior_buy = _make_order(id=1, order_type="buy", quantity="10", price="150", occurred_at=EARLY)
    existing = _make_holding(qty="10", avg_cost="150.00")
    svc = _make_service(
        existing_holding=existing, existing_orders=[prior_buy], cash_balance=Decimal("0")
    )
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
    prior_buy = _make_order(id=1, order_type="buy", quantity="10", price="150", occurred_at=EARLY)
    existing = _make_holding(qty="10", avg_cost="150.00")
    svc = _make_service(existing_holding=existing, existing_orders=[prior_buy])
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
    # History: buy 5, buy 5, sell 8. Deleting the first buy leaves buy 5 then
    # sell 8 — which goes negative at the sell.
    target = _make_order(
        id=1,
        order_type="buy",
        quantity="5",
        price="150",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    history = [
        target,
        _make_order(
            id=2,
            order_type="buy",
            quantity="5",
            price="160",
            occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
        ),
        _make_order(
            id=3,
            order_type="sell",
            quantity="8",
            price="170",
            occurred_at=datetime(2026, 1, 3, tzinfo=UTC),
        ),
    ]
    svc = _make_service()
    svc.order_repository.get_by_public_id = AsyncMock(return_value=target)
    svc.order_repository.list_by_holding = AsyncMock(return_value=history)

    with pytest.raises(ValidationError) as exc_info:
        await svc.delete_order(WS, USER, target.public_id)
    assert "negative holding" in exc_info.value.detail


@pytest.mark.asyncio
async def test_delete_order_recomputes_avg_cost_chronologically():
    # buy 10 @100, sell 10 @150, buy 10 @200 -> avg cost should be 200 (not 150).
    # Deleting an unrelated later order triggers a full chronological recompute.
    history = [
        _make_order(
            id=1,
            order_type="buy",
            quantity="10",
            price="100",
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        _make_order(
            id=2,
            order_type="sell",
            quantity="10",
            price="150",
            occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
        ),
        _make_order(
            id=3,
            order_type="buy",
            quantity="10",
            price="200",
            occurred_at=datetime(2026, 1, 3, tzinfo=UTC),
        ),
    ]
    target = _make_order(
        id=4,
        order_type="buy",
        quantity="1",
        price="999",
        occurred_at=datetime(2026, 1, 4, tzinfo=UTC),
    )

    svc = _make_service(existing_holding=_make_holding(qty="11", avg_cost="0"))
    svc.order_repository.get_by_public_id = AsyncMock(return_value=target)
    # First call (delete validation) sees all orders; after delete, recompute sees `history`.
    svc.order_repository.list_by_holding = AsyncMock(side_effect=[[*history, target], history])
    svc.order_repository.save = AsyncMock(side_effect=lambda o: o)

    saved: list[Holding] = []
    svc.holding_repository.save = AsyncMock(side_effect=lambda h: saved.append(h) or h)

    await svc.delete_order(WS, USER, target.public_id)

    assert saved, "expected holding to be recomputed and saved"
    recomputed = saved[-1]
    assert recomputed.quantity == Decimal("10")
    assert recomputed.avg_cost == Decimal("200.000000")
    # The sell's realized P&L is recorded against the cost basis at sale time (100).
    sell = next(o for o in history if o.order_type == "sell")
    assert sell.avg_cost_at_sale == Decimal("100.000000")
    assert sell.realized_gain_loss == Decimal("500.00")


# ---------------------------------------------------------------------------
# update_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_order_writes_valid_audit_log_with_before_and_after():
    target = _make_order(
        id=1,
        order_type="buy",
        quantity="10",
        price="150",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    svc = _make_service(existing_holding=_make_holding(qty="10", avg_cost="150.00"))
    svc.order_repository.get_by_public_id = AsyncMock(return_value=target)
    svc.order_repository.list_by_holding = AsyncMock(return_value=[target])
    svc.order_repository.save = AsyncMock(side_effect=lambda o: o)
    svc.holding_repository.get_by_unique_key = AsyncMock(
        return_value=_make_holding(qty="10", avg_cost="150.00")
    )

    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    audit_logger = AuditLogger(session)

    updated = await svc.update_order(
        WS,
        USER,
        target.public_id,
        InvestingOrderUpdate(quantity=Decimal("12")),
        audit_logger=audit_logger,
    )

    assert updated.quantity == Decimal("12")
    session.add.assert_called_once()
    audit_log = session.add.call_args[0][0]
    assert audit_log.details["before"]["quantity"] == "10"
    assert audit_log.details["after"]["quantity"] == "12"
    assert "quantity" in audit_log.details["changed_fields"]


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
