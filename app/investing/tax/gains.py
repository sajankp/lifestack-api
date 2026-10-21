"""FIFO lot replay and gain computation.

The replay mirrors ``InvestingOrderService._replay_orders`` (spec-044) but
works on plain data so the tax engine stays free of DB access. It must
agree with the service to the cent — the reconciliation guard in
``engine.py`` asserts exactly that.

Buy-side fees capitalise into the cost of the lot (Sec 48). Sell-side
fees reduce the proceeds of the units sold, not their cost.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.investing.tax.fy import add_months
from app.investing.tax.regime import RegimeSpec, RegimeType

MONEY_QUANT = Decimal("0.01")
COST_QUANT = Decimal("0.000001")
QTY_QUANT = Decimal("0.00000001")


@dataclass(frozen=True)
class OrderView:
    """The fields of an investment order the tax engine needs."""

    public_id: str
    order_type: str
    symbol: str
    quantity: Decimal
    price_per_unit: Decimal
    gross_amount: Decimal
    brokerage_fee: Decimal
    tax_amount: Decimal
    other_fees: Decimal
    occurred_at: datetime
    currency: str
    created_at: datetime | None = None
    """Row insertion time — the tie-breaker when two orders share a timestamp."""

    account_id: str = ""
    account_public_id: str = ""
    account_name: str = ""
    instrument_id: int | None = None
    """The internal id of the instrument the order refers to, when it has one."""
    instrument_type: str = "stock"
    realized_gain_loss: Decimal | None = None
    avg_cost_at_sale: Decimal | None = None


@dataclass
class OpenLot:
    """A buy lot still open in the replay."""

    quantity: Decimal
    cost_per_unit: Decimal
    acquired_at: datetime
    buy_order_id: str


@dataclass
class TaxLot:
    """One consumed lot of a sell, classified for tax."""

    symbol: str
    account_id: str
    account_name: str
    instrument_type: str
    currency: str
    quantity: Decimal
    cost_per_unit: Decimal
    acquired_at: datetime
    sold_at: datetime
    sale_price_per_unit: Decimal
    gain_native: Decimal
    fees_native: Decimal
    """This lot's pro-rata share of the sell's fees (proceeds reduction)."""

    regime: RegimeType
    holding_months: int
    is_long_term: bool
    long_term_mark_date: datetime
    classified_by: str
    classification_detail: str

    gain_inr: Decimal = Decimal("0")
    """The gain converted to INR at the Rule 115 rate for the sale month.

    It is zero until the engine converts the lot; the pure replay in this
    module has no FX rate.
    """


@dataclass
class GainTotals:
    """Aggregated gains for one bucket."""

    gain_native: Decimal = Decimal("0")
    fees_native: Decimal = Decimal("0")

    def add(self, gain: Decimal, fees: Decimal = Decimal("0")) -> None:
        self.gain_native += gain
        self.fees_native += fees


@dataclass
class SellReconciliation:
    """The recorded-vs-computed check for one sell."""

    public_id: str
    symbol: str
    computed_gain: Decimal
    recorded_gain: Decimal | None
    computed_cost_per_unit: Decimal | None
    recorded_cost_per_unit: Decimal | None

    @property
    def gain_matches(self) -> bool:
        if self.recorded_gain is None:
            return True
        return abs(self.computed_gain - self.recorded_gain) <= Decimal("0.02")

    @property
    def cost_matches(self) -> bool:
        if self.recorded_cost_per_unit is None or self.computed_cost_per_unit is None:
            return True
        return (
            abs(self.computed_cost_per_unit - self.recorded_cost_per_unit)
            <= Decimal("0.01")
        )

    @property
    def ok(self) -> bool:
        return self.gain_matches and self.cost_matches


@dataclass
class ReplayResult:
    """Everything a replay produced."""

    lots: list[TaxLot] = field(default_factory=list)
    reconciliations: list[SellReconciliation] = field(default_factory=list)
    open_lots: list[OpenLot] = field(default_factory=list)

    @property
    def all_reconciled(self) -> bool:
        return all(r.ok for r in self.reconciliations)

    @property
    def failures(self) -> list[SellReconciliation]:
        return [r for r in self.reconciliations if not r.ok]


def order_total_fees(order: OrderView) -> Decimal:
    return (order.brokerage_fee or Decimal("0")) + (
        order.tax_amount or Decimal("0")
    ) + (order.other_fees or Decimal("0"))


def effective_buy_cost_per_unit(order: OrderView) -> Decimal:
    """Per-unit cost of a buy, with buy-side fees capitalised in.

    Sec 48 capitalises the incidental costs of acquisition into the cost
    of the asset. Matches ``_effective_buy_cost_per_unit`` in the order
    service — the reconciliation guard depends on the two agreeing.
    """
    fees = order_total_fees(order)
    if fees == 0 or order.quantity == 0:
        return order.price_per_unit.quantize(COST_QUANT)
    return (
        order.price_per_unit + fees / order.quantity
    ).quantize(COST_QUANT)


def is_long_term(acquired_at: datetime, sold_at: datetime, regime: RegimeSpec) -> bool:
    """Whether a lot is long-term under the regime's threshold.

    "More than N months" means the holding must exceed N **complete
    calendar months** — strictly greater than acquisition + N months.
    A days-based approximation drifts and misclassifies boundary lots.
    """
    return sold_at > add_months(acquired_at, regime.ltcg_holding_months)


def _months_between(start: datetime, end: datetime) -> int:
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return months


def replay_fifo_lots(
    orders: list[OrderView],
    *,
    regime_resolver,
    fy_start: datetime,
    fy_end: datetime,
) -> ReplayResult:
    """Replay the orders chronologically, classifying each consumed lot.

    ``regime_resolver`` is called with the sell order and returns
    ``(regime_spec, classified_by, classification_detail)``.

    Only sells inside the FY produce lots; the replay still runs over the
    full history so earlier sells consume their lots correctly.
    """
    result = ReplayResult()
    queue: deque[OpenLot] = deque()

    # Sort by (occurred_at, created_at) to match the order service's
    # replay ordering. Two sells can share an occurred_at — the broker's
    # own recorded cost-per-unit shows which executed first, and that is
    # the created_at order. Ties beyond that fall back to public_id for
    # determinism.
    events = sorted(
        orders,
        key=lambda o: (
            o.occurred_at,
            o.created_at or o.occurred_at,
            o.public_id,
        ),
    )
    for order in events:
        if order.order_type == "buy":
            queue.append(
                OpenLot(
                    quantity=order.quantity,
                    cost_per_unit=effective_buy_cost_per_unit(order),
                    acquired_at=order.occurred_at,
                    buy_order_id=order.public_id,
                )
            )
            continue

        # Sell: consume the oldest open lots first (Sec 45(2A) FIFO).
        to_consume = order.quantity
        consumed: list[tuple[OpenLot, Decimal]] = []
        cost_consumed = Decimal("0")
        while to_consume > 0 and queue:
            lot = queue[0]
            take = min(lot.quantity, to_consume)
            consumed.append((lot, take))
            cost_consumed += take * lot.cost_per_unit
            lot.quantity -= take
            to_consume -= take
            if lot.quantity == 0:
                queue.popleft()

        computed_gain = (
            sum(
                (take * (order.price_per_unit - lot.cost_per_unit) for lot, take in consumed),
                Decimal("0"),
            )
            - order_total_fees(order)
        )
        computed_cost = (
            (cost_consumed / order.quantity).quantize(COST_QUANT)
            if order.quantity
            else None
        )
        result.reconciliations.append(
            SellReconciliation(
                public_id=order.public_id,
                symbol=order.symbol,
                computed_gain=computed_gain.quantize(MONEY_QUANT),
                recorded_gain=order.realized_gain_loss,
                computed_cost_per_unit=computed_cost,
                recorded_cost_per_unit=order.avg_cost_at_sale,
            )
        )

        if not (fy_start <= order.occurred_at <= fy_end):
            continue

        regime, classified_by, classification_detail = regime_resolver(order)
        sell_fees = order_total_fees(order)
        # Sell-side fees reduce proceeds, not cost. Spread them across the
        # lots this sell consumed, pro-rata by each lot's gross gain, so a
        # sell spanning both buckets allocates correctly.
        gross_gains = [
            take * (order.price_per_unit - lot.cost_per_unit) for lot, take in consumed
        ]
        gross_total = sum(gross_gains, Decimal("0"))
        for (lot, take), gross_gain in zip(consumed, gross_gains, strict=True):
            lot_fees = (
                (sell_fees * gross_gain / gross_total) if gross_total else Decimal("0")
            )
            long_term = is_long_term(lot.acquired_at, order.occurred_at, regime)
            result.lots.append(
                TaxLot(
                    symbol=order.symbol,
                    account_id=order.account_id,
                    account_name=order.account_name,
                    instrument_type=order.instrument_type,
                    currency=order.currency,
                    quantity=take,
                    cost_per_unit=lot.cost_per_unit,
                    acquired_at=lot.acquired_at,
                    sold_at=order.occurred_at,
                    sale_price_per_unit=order.price_per_unit,
                    gain_native=gross_gain.quantize(MONEY_QUANT),
                    fees_native=lot_fees.quantize(MONEY_QUANT),
                    regime=regime.key,
                    holding_months=_months_between(lot.acquired_at, order.occurred_at),
                    is_long_term=long_term,
                    long_term_mark_date=add_months(
                        lot.acquired_at, regime.ltcg_holding_months
                    ),
                    classified_by=classified_by,
                    classification_detail=classification_detail,
                )
            )

    result.open_lots = list(queue)
    return result
