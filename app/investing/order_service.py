"""Order placement, FIFO lot cost-basis logic, and order replay.

Split out of ``app/investing/service.py`` (chore/split-investing-service) —
pure move, no behavior change.
"""

import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal

from pydantic import ValidationError as PydanticValidationError

from app.core.audit import AuditLogger, snapshot_columns
from app.core.exceptions import NotFoundError, ValidationError
from app.core.pagination import DEFAULT_LIMIT
from app.finance.models import Account
from app.finance.repository import AccountRepository, CurrencyRepository
from app.investing.models import (
    CashBalance,
    CorporateAction,
    Holding,
    InvestingOrder,
    LotConsumption,
    OrderLot,
    OrderType,
)
from app.investing.repository import (
    CashBalanceRepository,
    CorporateActionRepository,
    HoldingRepository,
    InvestingOrderRepository,
    LotRepository,
)
from app.investing.schemas import CorporateActionCreate, InvestingOrderCreate, InvestingOrderUpdate
from app.investing.service import MONEY_QUANT, InstrumentService

AVG_COST_PRECISION = Decimal("0.000001")
# Matches the Numeric(18, 8) scale of every quantity column (InvestingOrder.quantity,
# OrderLot.original_quantity/remaining_quantity) — used to quantize a computed bonus-issue
# share count before it's persisted.
LOT_QTY_PRECISION = Decimal("0.00000001")


_ORDER_AUDIT_FIELDS = (
    "order_type",
    "symbol",
    "currency",
    "quantity",
    "price_per_unit",
    "gross_amount",
    "brokerage_fee",
    "tax_amount",
    "other_fees",
    "net_amount",
    "occurred_at",
    "exchange_name",
    "notes",
)


def _snapshot_order(order: InvestingOrder) -> dict:
    data = snapshot_columns(order, _ORDER_AUDIT_FIELDS)
    # Convert Decimal fields for JSON serialization
    for field_name in (
        "quantity",
        "price_per_unit",
        "gross_amount",
        "brokerage_fee",
        "tax_amount",
        "other_fees",
        "net_amount",
    ):
        if data.get(field_name) is not None:
            data[field_name] = str(data[field_name])
    # Convert datetime field for JSON serialization
    if data.get("occurred_at") is not None:
        data["occurred_at"] = (
            data["occurred_at"].isoformat()
            if hasattr(data["occurred_at"], "isoformat")
            else str(data["occurred_at"])
        )
    return data


def _order_total_fees(order: InvestingOrder) -> Decimal:
    """Sum of all transaction costs recorded on an order.

    The fee columns are non-nullable with a 0 default, but guard against
    ``None`` defensively so a malformed/legacy row can't raise here.
    """
    return (
        (order.brokerage_fee or Decimal("0"))
        + (order.tax_amount or Decimal("0"))
        + (order.other_fees or Decimal("0"))
    )


def _effective_buy_cost_per_unit(order: InvestingOrder) -> Decimal:
    """Per-unit cost basis of a buy, with fees capitalized into the lot.

    ``cost = price_per_unit + total_fees / quantity`` — the broker/tax
    convention of folding buy-side brokerage, tax, and other fees into the
    asset's cost of acquisition (Section 48, Income-tax Act 1961). See
    spec-046. Sell-side fees are handled separately (they reduce realized
    proceeds, not the cost of units sold).
    """
    fees = _order_total_fees(order)
    if fees == 0 or order.quantity == 0:
        return order.price_per_unit
    return (order.price_per_unit + fees / order.quantity).quantize(AVG_COST_PRECISION)


@dataclass
class _OpenLot:
    """In-memory FIFO lot state during a ``_replay_orders`` pass.

    ``lot_key`` identifies the lot within a single replay pass — the buy
    order's id for a buy-derived lot, or ``f"bonus:{corporate_action.id}"``
    for a bonus-issue lot (which has no originating buy order). Exactly one
    of ``buy_order_id``/``corporate_action_id`` is set, matching the
    ``OrderLot`` CHECK constraint (spec-051).
    """

    lot_key: int | str
    buy_order_id: int | None
    corporate_action_id: int | None
    original_quantity: Decimal
    remaining: Decimal
    cost_per_unit: Decimal
    acquired_at: datetime


@dataclass
class _LotConsumptionEvent:
    sell_order_id: int
    lot_key: int | str
    quantity_consumed: Decimal
    cost_per_unit: Decimal


@dataclass
class _ReplayResult:
    quantity: Decimal
    avg_cost: Decimal
    lots: list[_OpenLot] = field(default_factory=list)
    consumptions: list[_LotConsumptionEvent] = field(default_factory=list)


class InvestingOrderService:
    def __init__(
        self,
        order_repository: InvestingOrderRepository,
        holding_repository: HoldingRepository,
        cash_balance_repository: CashBalanceRepository,
        account_repository: AccountRepository,
        currency_repository: CurrencyRepository,
        instrument_service: InstrumentService,
        lot_repository: LotRepository,
        corporate_action_repository: CorporateActionRepository,
    ):
        self.order_repository = order_repository
        self.holding_repository = holding_repository
        self.cash_balance_repository = cash_balance_repository
        self.account_repository = account_repository
        self.currency_repository = currency_repository
        self.instrument_service = instrument_service
        self.lot_repository = lot_repository
        self.corporate_action_repository = corporate_action_repository

    async def _validate_brokerage_account(
        self, workspace_id: int, account_public_id: uuid.UUID, currency: str | None = None
    ) -> "Account":
        account = await self.account_repository.get_by_public_id(workspace_id, account_public_id)
        if not account or not account.is_active:
            raise NotFoundError(
                detail=f"Account with id {account_public_id} not found in this workspace"
            )
        if account.account_type != "brokerage":
            raise ValidationError(
                detail=(
                    f"Orders can only be placed against brokerage accounts. "
                    f"Account '{account.name}' is type '{account.account_type}'"
                )
            )
        # One account, one currency (spec-050)
        if currency is not None and currency.upper() != account.default_currency_code.upper():
            raise ValidationError(
                detail=(
                    f"Currency '{currency.upper()}' does not match account '{account.name}' "
                    f"({account.default_currency_code})"
                )
            )
        return account

    async def _get_cash_balance(self, workspace_id: int, account_id: int, currency: str) -> Decimal:
        latest = await self.cash_balance_repository.get_latest_for_account_currency(
            workspace_id, account_id, currency
        )
        return latest.balance if latest is not None else Decimal("0")

    async def _update_cash_balance(
        self,
        workspace_id: int,
        user_id: int,
        account_id: int,
        currency: str,
        delta: Decimal,
        trigger_type: str,
        trigger_ref: uuid.UUID,
    ) -> CashBalance:
        current = await self._get_cash_balance(workspace_id, account_id, currency)
        new_balance = current + delta
        cash = CashBalance(
            workspace_id=workspace_id,
            user_id=user_id,
            account_id=account_id,
            balance=new_balance,
            currency=currency,
            as_of=datetime.now(UTC),
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
        )
        return await self.cash_balance_repository.create(cash)

    def _replay_orders(
        self,
        orders: Sequence[InvestingOrder],
        corporate_actions: Sequence[CorporateAction] = (),
        *,
        record_sells: bool,
    ) -> "_ReplayResult":
        """Replay orders and corporate actions chronologically with a FIFO
        lot cost-basis model.

        A sell is matched against the *oldest* open buy lot(s) first (FIFO),
        per Section 45(2A) of the Income-tax Act, 1961 and CBDT Circular 768
        — see spec-044. Returns the resulting position plus the lot/consumption
        state needed to persist ``OrderLot``/``LotConsumption`` rows. When
        ``record_sells`` is True, each sell order's ``realized_gain_loss`` and
        ``avg_cost_at_sale`` are updated in place against the lots it consumed.
        Raises ``ValidationError`` if a sell would exceed open lots at any
        intermediate point, not just in the final aggregate.

        Corporate actions (spec-051) are merged into the same chronological
        stream, ordered by ``ex_date``. A corporate action on the same
        calendar date as an order is applied *first* — splits/bonus
        allotments take effect before market open, so same-day trades already
        see the post-action price/quantity.
        """
        # `queue` drives FIFO consumption order; `all_lots` retains every lot
        # (including fully-consumed ones) so their final remaining_quantity=0
        # state and consumption history are still persisted.
        queue: deque[_OpenLot] = deque()
        all_lots: dict[int | str, _OpenLot] = {}
        consumptions: list[_LotConsumptionEvent] = []

        # order.occurred_at is a DateTime(timezone=True) column and always comes
        # back tz-aware when read from Postgres, but normalize defensively so an
        # in-memory naive datetime (e.g. from a not-yet-persisted simulated order
        # in update_order) can't raise a naive/aware comparison TypeError here.
        events: list[tuple[datetime, int, InvestingOrder | CorporateAction]] = [
            (
                order.occurred_at
                if order.occurred_at.tzinfo
                else order.occurred_at.replace(tzinfo=UTC),
                1,
                order,
            )
            for order in orders
        ] + [
            (datetime.combine(action.ex_date, time.min, tzinfo=UTC), 0, action)
            for action in corporate_actions
        ]
        events.sort(key=lambda e: (e[0], e[1], e[2].id or 0))

        for _when, _priority, event in events:
            if isinstance(event, CorporateAction):
                action = event
                assert action.id is not None
                if action.action_type == "split":
                    factor_qty = action.ratio_quote / action.ratio_base
                    factor_cost = action.ratio_base / action.ratio_quote
                    for lot in all_lots.values():
                        lot.original_quantity = (lot.original_quantity * factor_qty).quantize(
                            LOT_QTY_PRECISION
                        )
                        lot.remaining = (lot.remaining * factor_qty).quantize(LOT_QTY_PRECISION)
                        lot.cost_per_unit = (lot.cost_per_unit * factor_cost).quantize(
                            AVG_COST_PRECISION
                        )
                elif action.action_type == "bonus":
                    held_qty = sum((lot.remaining for lot in queue), Decimal("0"))
                    bonus_qty = (held_qty * action.ratio_quote / action.ratio_base).quantize(
                        LOT_QTY_PRECISION
                    )
                    if bonus_qty > 0:
                        bonus_key = f"bonus:{action.id}"
                        bonus_lot = _OpenLot(
                            lot_key=bonus_key,
                            buy_order_id=None,
                            corporate_action_id=action.id,
                            original_quantity=bonus_qty,
                            remaining=bonus_qty,
                            cost_per_unit=Decimal("0"),
                            acquired_at=datetime.combine(action.ex_date, time.min, tzinfo=UTC),
                        )
                        queue.append(bonus_lot)
                        all_lots[bonus_key] = bonus_lot
                continue

            order = event
            if order.order_type == "buy":
                assert order.id is not None
                lot = _OpenLot(
                    lot_key=order.id,
                    buy_order_id=order.id,
                    corporate_action_id=None,
                    original_quantity=order.quantity,
                    remaining=order.quantity,
                    cost_per_unit=_effective_buy_cost_per_unit(order),
                    acquired_at=order.occurred_at,
                )
                queue.append(lot)
                all_lots[lot.lot_key] = lot
            else:
                assert order.id is not None
                to_consume = order.quantity
                realized = Decimal("0")
                cost_consumed = Decimal("0")
                while to_consume > 0:
                    if not queue:
                        raise ValidationError(
                            detail=(
                                "This order would result in a negative holding "
                                "(a sell would exceed shares held at that point in time)"
                            )
                        )
                    lot = queue[0]
                    take = min(lot.remaining, to_consume)
                    realized += take * (order.price_per_unit - lot.cost_per_unit)
                    cost_consumed += take * lot.cost_per_unit
                    consumptions.append(
                        _LotConsumptionEvent(
                            sell_order_id=order.id,
                            lot_key=lot.lot_key,
                            quantity_consumed=take,
                            cost_per_unit=lot.cost_per_unit,
                        )
                    )
                    lot.remaining -= take
                    to_consume -= take
                    if lot.remaining == 0:
                        queue.popleft()
                if record_sells:
                    # Sell-side fees reduce realized proceeds (they are not
                    # added to the cost of the units sold). avg_cost_at_sale
                    # stays the fee-inclusive buy cost of the consumed lots.
                    realized -= _order_total_fees(order)
                    order.realized_gain_loss = realized.quantize(MONEY_QUANT)
                    order.avg_cost_at_sale = (cost_consumed / order.quantity).quantize(
                        AVG_COST_PRECISION
                    )

        final_qty = sum((lot.remaining for lot in queue), Decimal("0"))
        final_avg_cost = (
            (
                sum((lot.remaining * lot.cost_per_unit for lot in queue), Decimal("0")) / final_qty
            ).quantize(AVG_COST_PRECISION)
            if final_qty > 0
            else Decimal("0")
        )
        return _ReplayResult(
            quantity=final_qty,
            avg_cost=final_avg_cost,
            lots=list(all_lots.values()),
            consumptions=consumptions,
        )

    async def _recompute_holding(
        self, workspace_id: int, user_id: int, symbol: str, account_id: int
    ) -> Holding | None:
        orders = await self.order_repository.list_by_holding(workspace_id, symbol, account_id)
        corporate_actions = await self.corporate_action_repository.list_by_holding(
            workspace_id, symbol, account_id
        )
        holding = await self.holding_repository.get_by_unique_key(workspace_id, symbol, account_id)

        if not orders:
            # No orders remain; delete the holding if it exists. A corporate
            # action with no underlying orders has nothing to act on.
            if holding is not None:
                await self.holding_repository.delete(holding)
            return None

        if holding is not None and holding.id is not None:
            await self.lot_repository.delete_for_holding(holding.id)

        result = self._replay_orders(orders, corporate_actions, record_sells=True)

        # `orders` were loaded via this same session (list_by_holding), so the
        # in-place realized_gain_loss/avg_cost_at_sale mutations above are
        # already tracked as dirty and will be persisted on the next flush
        # (triggered below by the holding/lot repository calls) — no explicit
        # per-order save() needed.

        if holding is None:
            instrument_id = next((o.instrument_id for o in orders if o.instrument_id), None)
            holding = await self.holding_repository.create(
                Holding(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    symbol=symbol,
                    account_id=account_id,
                    quantity=result.quantity,
                    avg_cost=result.avg_cost,
                    currency=orders[0].currency,
                    source_type="order",
                    instrument_id=instrument_id,
                )
            )
        else:
            holding.quantity = result.quantity
            holding.avg_cost = result.avg_cost
            holding.updated_at = datetime.now(UTC)
            holding = await self.holding_repository.save(holding)

        assert holding.id is not None
        if result.lots:
            created_lots = await self.lot_repository.create_lots([
                OrderLot(
                    workspace_id=workspace_id,
                    holding_id=holding.id,
                    buy_order_id=lot.buy_order_id,
                    corporate_action_id=lot.corporate_action_id,
                    original_quantity=lot.original_quantity,
                    remaining_quantity=lot.remaining,
                    cost_per_unit=lot.cost_per_unit,
                    acquired_at=lot.acquired_at,
                )
                for lot in result.lots
            ])
            if result.consumptions:
                # `created_lots` was built from `result.lots` in the same order
                # (one row per source lot), so zip preserves the lot_key -> id
                # mapping without needing a lot_key column on OrderLot itself.
                lot_id_by_key = {
                    src.lot_key: created.id
                    for src, created in zip(result.lots, created_lots, strict=True)
                }
                await self.lot_repository.create_consumptions([
                    LotConsumption(
                        sell_order_id=ev.sell_order_id,
                        lot_id=lot_id_by_key[ev.lot_key],
                        quantity_consumed=ev.quantity_consumed,
                        cost_per_unit=ev.cost_per_unit,
                    )
                    for ev in result.consumptions
                ])
        return holding

    async def place_order(
        self,
        workspace_id: int,
        user_id: int,
        order_in: InvestingOrderCreate,
        audit_logger: AuditLogger | None = None,
        source_type: str = "manual",
    ) -> InvestingOrder:
        account = await self._validate_brokerage_account(
            workspace_id, order_in.account_id, order_in.currency
        )
        assert account.id is not None

        gross_amount = order_in.quantity * order_in.price_per_unit
        total_fees = order_in.brokerage_fee + order_in.tax_amount + order_in.other_fees

        if order_in.order_type == OrderType.buy:
            net_amount = gross_amount + total_fees
        else:
            net_amount = gross_amount - total_fees

        if order_in.order_type == OrderType.buy:
            available_cash = await self._get_cash_balance(
                workspace_id, account.id, order_in.currency
            )
            if available_cash < net_amount:
                raise ValidationError(
                    detail=(
                        f"Insufficient cash balance. "
                        f"Available: {order_in.currency} {available_cash:.2f}, "
                        f"Required: {order_in.currency} {net_amount:.2f}"
                    )
                )

        holding = await self.holding_repository.get_by_unique_key(
            workspace_id, order_in.symbol, account.id
        )

        if order_in.order_type == OrderType.sell and (
            holding is None or holding.quantity < order_in.quantity
        ):
            current_qty = holding.quantity if holding else Decimal("0")
            raise ValidationError(
                detail=(
                    f"Cannot sell {order_in.quantity} shares of {order_in.symbol}. "
                    f"Current holding: {current_qty} shares"
                )
            )

        instrument = await self.instrument_service.find_or_create_instrument(
            workspace_id,
            order_in.symbol,
            order_in.instrument_type,
            order_in.instrument_name,
        )

        order = InvestingOrder(
            workspace_id=workspace_id,
            user_id=user_id,
            account_id=account.id,
            order_type=order_in.order_type.value,
            symbol=order_in.symbol,
            instrument_id=instrument.id if instrument else None,
            quantity=order_in.quantity,
            price_per_unit=order_in.price_per_unit,
            gross_amount=gross_amount.quantize(MONEY_QUANT),
            brokerage_fee=order_in.brokerage_fee,
            tax_amount=order_in.tax_amount,
            other_fees=order_in.other_fees,
            net_amount=net_amount.quantize(MONEY_QUANT),
            currency=order_in.currency,
            exchange_name=order_in.exchange_name,
            occurred_at=order_in.occurred_at,
            notes=order_in.notes,
            source_type=source_type,
        )
        order = await self.order_repository.create(order)

        # Recompute holding quantity, FIFO avg_cost, and (for a sell) this
        # order's own realized_gain_loss/avg_cost_at_sale from the full order
        # history — the single replay path shared with update_order/delete_order.
        # `order` is the same session-tracked instance _recompute_holding will
        # load and mutate via the identity map, so no re-fetch is needed.
        await self._recompute_holding(workspace_id, user_id, order_in.symbol, account.id)

        # Update cash balance
        cash_delta = -net_amount if order_in.order_type == OrderType.buy else net_amount
        await self._update_cash_balance(
            workspace_id=workspace_id,
            user_id=user_id,
            account_id=account.id,
            currency=order_in.currency,
            delta=cash_delta,
            trigger_type="order",
            trigger_ref=order.public_id,
        )

        if audit_logger:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action=f"investing_order_{order_in.order_type.value}",
                module="investing",
                entity_type="investing_order",
                entity_id=order.id or 0,
                details={
                    "entity_public_id": str(order.public_id),
                    "before": None,
                    "after": {
                        "order_type": order_in.order_type.value,
                        "symbol": order_in.symbol,
                        "quantity": str(order_in.quantity),
                        "price_per_unit": str(order_in.price_per_unit),
                        "net_amount": str(net_amount),
                        "currency": order_in.currency,
                    },
                    "changed_fields": ["quantity", "avg_cost", "cash_balance"],
                },
            )

        return order

    async def delete_order(
        self,
        workspace_id: int,
        user_id: int,
        order_public_id: uuid.UUID,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        order = await self.order_repository.get_by_public_id(workspace_id, order_public_id)
        if not order:
            raise NotFoundError(
                detail=f"Order with id {order_public_id} not found in this workspace"
            )

        # Replay the remaining orders chronologically to ensure deleting this one
        # never drives the holding negative at any point in time (not just in the
        # final aggregate). Corporate actions must be included here too — a sell
        # that's only valid post-split would otherwise look like it exceeds the
        # (pre-split) holding.
        remaining_orders = [
            o
            for o in await self.order_repository.list_by_holding(
                workspace_id, order.symbol, order.account_id
            )
            if o.public_id != order.public_id
        ]
        corporate_actions = await self.corporate_action_repository.list_by_holding(
            workspace_id, order.symbol, order.account_id
        )
        self._replay_orders(remaining_orders, corporate_actions, record_sells=False)

        # Reverse the cash balance impact
        cash_delta = order.net_amount if order.order_type == "buy" else -order.net_amount
        await self._update_cash_balance(
            workspace_id=workspace_id,
            user_id=user_id,
            account_id=order.account_id,
            currency=order.currency,
            delta=cash_delta,
            trigger_type="order",
            trigger_ref=order.public_id,
        )

        await self.order_repository.delete(order)

        # Recompute holding from remaining orders
        await self._recompute_holding(workspace_id, user_id, order.symbol, order.account_id)

        if audit_logger:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="investing_order_deleted",
                module="investing",
                entity_type="investing_order",
                entity_id=order.id or 0,
                details={
                    "entity_public_id": str(order.public_id),
                    "before": {
                        "order_type": order.order_type,
                        "symbol": order.symbol,
                        "quantity": str(order.quantity),
                    },
                    "after": None,
                    "changed_fields": [],
                },
            )

    async def update_order(
        self,
        workspace_id: int,
        user_id: int,
        order_public_id: uuid.UUID,
        order_update: InvestingOrderUpdate,
        audit_logger: AuditLogger | None = None,
    ) -> InvestingOrder:
        order = await self.order_repository.get_by_public_id(workspace_id, order_public_id)
        if not order:
            raise NotFoundError(
                detail=f"Order with id {order_public_id} not found in this workspace"
            )
        before_snap = _snapshot_order(order)

        # Resolve updated values (None means keep existing)
        new_order_type = (
            order_update.order_type.value
            if order_update.order_type is not None
            else order.order_type
        )
        new_quantity = (
            order_update.quantity if order_update.quantity is not None else order.quantity
        )
        new_price = (
            order_update.price_per_unit
            if order_update.price_per_unit is not None
            else order.price_per_unit
        )
        new_brokerage_fee = (
            order_update.brokerage_fee
            if order_update.brokerage_fee is not None
            else order.brokerage_fee
        )
        new_tax_amount = (
            order_update.tax_amount if order_update.tax_amount is not None else order.tax_amount
        )
        new_other_fees = (
            order_update.other_fees if order_update.other_fees is not None else order.other_fees
        )
        new_occurred_at = (
            order_update.occurred_at if order_update.occurred_at is not None else order.occurred_at
        )

        new_gross = new_quantity * new_price
        new_total_fees = new_brokerage_fee + new_tax_amount + new_other_fees
        new_net = (
            new_gross + new_total_fees if new_order_type == "buy" else new_gross - new_total_fees
        ).quantize(MONEY_QUANT)

        # Validate replay with the updated order substituted in place before touching DB
        all_orders = await self.order_repository.list_by_holding(
            workspace_id, order.symbol, order.account_id
        )
        simulated_orders: list[InvestingOrder] = []
        for o in all_orders:
            if o.id == order.id:
                # Replace with in-memory copy reflecting proposed edits
                try:
                    sim = o.model_copy(
                        update={
                            "order_type": new_order_type,
                            "quantity": new_quantity,
                            "price_per_unit": new_price,
                            "occurred_at": new_occurred_at,
                        }
                    )
                except PydanticValidationError as exc:
                    raise ValidationError(detail=f"Invalid order update values: {exc}") from exc
                simulated_orders.append(sim)
            else:
                simulated_orders.append(o)
        # Re-sort by occurred_at since the date may have changed
        simulated_orders.sort(key=lambda o: (o.occurred_at, o.id or 0))
        corporate_actions = await self.corporate_action_repository.list_by_holding(
            workspace_id, order.symbol, order.account_id
        )
        self._replay_orders(simulated_orders, corporate_actions, record_sells=False)

        # Reverse old cash impact and apply the new cash impact in a single
        # combined delta so an edit produces exactly one CashBalance snapshot
        # row instead of two (one for the reversal, one for the new impact).
        old_cash_reversal = order.net_amount if order.order_type == "buy" else -order.net_amount
        new_cash_delta = -new_net if new_order_type == "buy" else new_net
        combined_cash_delta = old_cash_reversal + new_cash_delta

        # For buy orders validate sufficient cash as-if the reversal had already
        # been applied, without actually writing it to the DB yet.
        if new_order_type == "buy":
            current_cash = await self._get_cash_balance(
                workspace_id, order.account_id, order.currency
            )
            available_cash = current_cash + old_cash_reversal
            if available_cash < new_net:
                raise ValidationError(
                    detail=(
                        f"Insufficient cash balance. "
                        f"Available: {order.currency} {available_cash:.2f}, "
                        f"Required: {order.currency} {new_net:.2f}"
                    )
                )

        # Persist updated order fields
        order.order_type = new_order_type
        order.quantity = new_quantity
        order.price_per_unit = new_price
        order.brokerage_fee = new_brokerage_fee
        order.tax_amount = new_tax_amount
        order.other_fees = new_other_fees
        order.gross_amount = new_gross.quantize(MONEY_QUANT)
        order.net_amount = new_net
        order.occurred_at = new_occurred_at
        if order_update.exchange_name is not None:
            order.exchange_name = order_update.exchange_name
        if "notes" in order_update.model_fields_set:
            order.notes = order_update.notes
        order.updated_at = datetime.now(UTC)
        order = await self.order_repository.save(order)

        # Apply the combined cash impact in a single snapshot write
        await self._update_cash_balance(
            workspace_id=workspace_id,
            user_id=user_id,
            account_id=order.account_id,
            currency=order.currency,
            delta=combined_cash_delta,
            trigger_type="order",
            trigger_ref=order.public_id,
        )

        # Recompute holding quantity and avg cost from full order history
        await self._recompute_holding(workspace_id, user_id, order.symbol, order.account_id)

        if audit_logger:
            after_snap = _snapshot_order(order)
            changed_fields = [k for k in before_snap if before_snap[k] != after_snap[k]]
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="investing_order_updated",
                module="investing",
                entity_type="investing_order",
                entity_id=order.id or 0,
                details={
                    "entity_public_id": str(order.public_id),
                    "before": before_snap,
                    "after": after_snap,
                    "changed_fields": changed_fields,
                },
            )

        return order

    async def bulk_import_orders(
        self,
        workspace_id: int,
        user_id: int,
        orders: list[InvestingOrderCreate],
        source_import_id: int | None,
        audit_logger: AuditLogger | None = None,
    ) -> list[InvestingOrder]:
        sorted_orders = sorted(orders, key=lambda o: o.occurred_at)
        created = []
        for order_in in sorted_orders:
            order = await self.place_order(
                workspace_id=workspace_id,
                user_id=user_id,
                order_in=order_in,
                audit_logger=audit_logger,
                source_type="csv_import",
            )
            if source_import_id is not None:
                order.source_import_id = source_import_id
            created.append(order)
        return created

    async def list_orders(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        symbol: str | None = None,
        account_id: int | None = None,
        order_type: str | None = None,
        search: str | None = None,
    ) -> tuple[Sequence[InvestingOrder], int]:
        return await self.order_repository.list_by_workspace(
            workspace_id, limit, offset, symbol, account_id, order_type, search
        )

    async def get_order(self, workspace_id: int, public_id: uuid.UUID) -> InvestingOrder:
        order = await self.order_repository.get_by_public_id(workspace_id, public_id)
        if not order:
            raise NotFoundError(detail=f"Order with id {public_id} not found in this workspace")
        return order

    async def list_orders_for_holding(
        self, workspace_id: int, symbol: str, account_id: int
    ) -> Sequence[InvestingOrder]:
        return await self.order_repository.list_by_holding(workspace_id, symbol, account_id)

    async def create_corporate_action(
        self,
        workspace_id: int,
        user_id: int,
        action_in: CorporateActionCreate,
        audit_logger: AuditLogger | None = None,
    ) -> CorporateAction:
        account = await self._validate_brokerage_account(workspace_id, action_in.account_id)
        assert account.id is not None

        action = CorporateAction(
            workspace_id=workspace_id,
            user_id=user_id,
            account_id=account.id,
            symbol=action_in.symbol,
            action_type=action_in.action_type.value,
            ratio_base=action_in.ratio_base,
            ratio_quote=action_in.ratio_quote,
            ex_date=action_in.ex_date,
            notes=action_in.notes,
        )
        action = await self.corporate_action_repository.create(action)

        # Cash-neutral by construction: no _update_cash_balance call, no new
        # investing_cash_balances row (spec-051).
        await self._recompute_holding(workspace_id, user_id, action_in.symbol, account.id)

        if audit_logger:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action=f"investing_corporate_action_{action_in.action_type.value}",
                module="investing",
                entity_type="corporate_action",
                entity_id=action.id or 0,
                details={
                    "entity_public_id": str(action.public_id),
                    "before": None,
                    "after": {
                        "action_type": action_in.action_type.value,
                        "symbol": action_in.symbol,
                        "ratio_base": str(action_in.ratio_base),
                        "ratio_quote": str(action_in.ratio_quote),
                        "ex_date": action_in.ex_date.isoformat(),
                    },
                    "changed_fields": ["quantity", "avg_cost"],
                },
            )

        return action

    async def list_corporate_actions(
        self,
        workspace_id: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        symbol: str | None = None,
        account_id: int | None = None,
    ) -> tuple[Sequence[CorporateAction], int]:
        return await self.corporate_action_repository.list_by_workspace(
            workspace_id, limit, offset, symbol=symbol, account_id=account_id
        )

    async def delete_corporate_action(
        self,
        workspace_id: int,
        user_id: int,
        action_public_id: uuid.UUID,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        action = await self.corporate_action_repository.get_by_public_id(
            workspace_id, action_public_id
        )
        if not action:
            raise NotFoundError(
                detail=f"Corporate action with id {action_public_id} not found in this workspace"
            )

        # Extract everything needed below before delete() flushes the row —
        # the ORM instance transitions to "deleted" state after flush, and
        # accessing an attribute not already loaded can raise ObjectDeletedError.
        symbol = action.symbol
        account_id = action.account_id
        action_id = action.id or 0
        public_id = action.public_id
        action_type = action.action_type
        ratio_base = action.ratio_base
        ratio_quote = action.ratio_quote

        await self.corporate_action_repository.delete(action)
        await self._recompute_holding(workspace_id, user_id, symbol, account_id)

        if audit_logger:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="investing_corporate_action_deleted",
                module="investing",
                entity_type="corporate_action",
                entity_id=action_id,
                details={
                    "entity_public_id": str(public_id),
                    "before": {
                        "action_type": action_type,
                        "symbol": symbol,
                        "ratio_base": str(ratio_base),
                        "ratio_quote": str(ratio_quote),
                    },
                    "after": None,
                    "changed_fields": [],
                },
            )
