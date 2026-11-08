"""Read-only capital-gains tax what-if simulations (spec-099 extension).

``simulate_sale``, ``list_tax_lots`` and ``harvest_candidates`` are pure:
they take plain-data snapshots and return plain data. They never open a DB
session and never mutate a holding — they answer "what if I sell N units
of X on date D at price P?" for the user.

The tax numbers reuse the same engine used for the real estimate
(``compute_tax_estimate``), so a simulation and a filing apply identical
statutory rules (FIFO cost basis, exact 12/24-month holding thresholds,
Rule 115 FX, slab/surcharge/cess/rebate). One deliberate difference: a
simulation runs in isolation, so the full LTCG exemption is applied to the
simulated sale as if it were the FY's only long-term gain — that is called
out in the result's assumptions, and no tax figure is presented when the
Rule 115 FX rate is unavailable (mirroring the engine's reconciliation
guard).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from app.investing.tax.engine import (
    compute_tax_estimate,
    sale_month_key,
    specified_fx_date,
)
from app.investing.tax.fy import add_months, fy_label_for_date, months_between
from app.investing.tax.gains import OrderView, is_long_term, order_total_fees
from app.investing.tax.regime import REGIMES as REGIME_SPECS
from app.investing.tax.regime import RegimeSpec, RegimeType
from app.investing.tax.tax_profile import TaxProfile

MONEY = Decimal("0.01")
ZERO = Decimal("0")


def _percent_label(rate: Decimal | None) -> str:
    """Render a rate as a percentage string without trailing zeros or
    exponent notation (30.00 -> "30%", 12.50 -> "12.5%")."""
    if rate is None:
        return "marginal slab rate"
    text = format(rate * Decimal("100"), "f")
    return f"{text.rstrip('0').rstrip('.')}%"


# --------------------------------------------------------------------------- #
# Input domain (read-only snapshots; a service/MCP adapter builds these from
# the SQLModel Holding + its open OrderLot rows).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LotSnapshot:
    """One open FIFO lot of a holding (read-only snapshot)."""

    lot_id: int | None
    """Database id of the OrderLot, or None for a synthetic lot."""

    quantity: Decimal
    """Remaining (open) quantity of the lot."""

    cost_per_unit: Decimal
    """Fee-inclusive average cost per unit (Sec 48 capitalisation applied)."""

    acquired_at: datetime
    """When the lot was acquired (timezone-aware)."""


@dataclass(frozen=True)
class HoldingSnapshot:
    """A holding's tax-relevant state, read-only.

    ``lots`` are the open lots, oldest acquisition first.
    """

    holding_id: int
    symbol: str
    account_id: str
    account_name: str
    currency: str
    regime: RegimeType
    regime_source: str
    """``ClassificationSource`` value that chose the regime, for audit display."""
    lots: tuple[LotSnapshot, ...] = ()


# --------------------------------------------------------------------------- #
# simulate_sale
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConsumedLot:
    """One open lot consumed (in part or in full) by a simulated sale."""

    lot_id: int | None
    quantity: Decimal
    cost_per_unit: Decimal
    acquired_at: datetime
    sale_price_per_unit: Decimal
    gain_native: Decimal
    """Gross gain of this chunk in the holding's native currency."""
    fees_native: Decimal
    """This chunk's pro-rata share of the hypothetical sell fees."""
    gain_inr: Decimal | None
    """Gross gain converted at the Rule 115 rate; None when no FX rate."""
    regime: RegimeType
    holding_months: int
    is_long_term: bool
    days_to_long_term: int
    """Calendar days remaining until the lot turns long-term (0 = already
    long-term, or crosses into long-term the day after the sale date)."""
    long_term_mark_date: datetime


@dataclass(frozen=True)
class SaleSimulation:
    """Result of a read-only simulated sale."""

    holding_id: int
    symbol: str
    regime: RegimeType
    sale_date: datetime
    sale_price: Decimal
    currency: str
    quantity_sold: Decimal
    quantity_consumed: Decimal
    """How much of the request the open lots can actually cover."""
    status: str
    """``success`` | ``insufficient_quantity`` | ``missing_fx_rate``."""
    consumed_lots: list[ConsumedLot] = field(default_factory=list)
    gain_native: Decimal = ZERO
    ltcg_gain_native: Decimal = ZERO
    stcg_gain_native: Decimal = ZERO
    gain_inr: Decimal | None = None
    tax_inr: Decimal | None = None
    fy_label: str = ""
    fx_rate: Decimal | None = None
    fx_source: str = ""
    assumptions: list[str] = field(default_factory=list)
    message: str | None = None


def simulate_sale(
    holding: HoldingSnapshot,
    *,
    qty: Decimal,
    sale_date: datetime,
    sale_price: Decimal,
    profile: TaxProfile,
    app_fx_rates: dict[tuple[str, str], Decimal] | None = None,
    sell_fees: Decimal = ZERO,
) -> SaleSimulation:
    """Simulate selling ``qty`` units at ``sale_price`` on ``sale_date``.

    Consumes the holding's open lots FIFO (oldest first), classifies each
    consumed chunk long-term/short-term under the holding's regime, and
    prices the result through :func:`compute_tax_estimate` using synthetic
    buy + sell OrderViews — so the LTCG/STCG split, the Rule 115 FX rate
    and its source, and the estimated tax all come from the same engine
    the real estimate uses.
    """
    if qty <= 0:
        raise ValueError("qty must be positive")
    if sale_price < 0:
        raise ValueError("sale_price must not be negative")

    regime_spec: RegimeSpec = REGIME_SPECS[holding.regime]
    currency = holding.currency.upper()

    # Open lots, oldest acquisition first (stable for equal timestamps).
    lots = sorted(holding.lots, key=lambda lot: (lot.acquired_at, str(lot.lot_id)))
    total_open = sum((lot.quantity for lot in lots), ZERO)
    insufficient = qty > total_open
    to_consume = total_open if insufficient else qty

    # --- FIFO consumption over the open lots ----------------------------- #
    consumed: list[tuple[LotSnapshot, Decimal]] = []
    remaining = to_consume
    for lot in lots:
        if remaining <= 0:
            break
        take = min(lot.quantity, remaining)
        consumed.append((lot, take))
        remaining -= take

    # --- Per-lot tax classification -------------------------------------- #
    chunks: list[dict] = []
    for lot, take in consumed:
        mark = add_months(lot.acquired_at, regime_spec.ltcg_holding_months)
        long_term = is_long_term(lot.acquired_at, sale_date, regime_spec)
        delta_days = (mark.date() - sale_date.date()).days
        days_to_lt = 0 if long_term else max(delta_days, 0)
        chunks.append({
            "lot": lot,
            "take": take,
            "gross_gain": take * (sale_price - lot.cost_per_unit),
            "long_term": long_term,
            "holding_months": months_between(lot.acquired_at, sale_date),
            "days_to_long_term": days_to_lt,
            "long_term_mark_date": mark,
        })

    # Sell-side fees reduce proceeds, spread pro-rata by each chunk's
    # gross gain (identical allocation to engine.py's replay).
    gross_total = sum((c["gross_gain"] for c in chunks), ZERO)
    sell_fees_total = order_total_fees(
        OrderView(
            public_id="sim-sell-fees",
            order_type="sell",
            symbol=holding.symbol,
            quantity=to_consume,
            price_per_unit=sale_price,
            gross_amount=ZERO,
            brokerage_fee=sell_fees,
            tax_amount=ZERO,
            other_fees=ZERO,
            occurred_at=sale_date,
            currency=currency,
        )
    )
    base_lots: list[ConsumedLot] = []
    for c in chunks:
        lot_fees = (
            (sell_fees_total * c["gross_gain"] / gross_total).quantize(MONEY)
            if gross_total
            else ZERO
        )
        base_lots.append(
            ConsumedLot(
                lot_id=c["lot"].lot_id,
                quantity=c["take"],
                cost_per_unit=c["lot"].cost_per_unit,
                acquired_at=c["lot"].acquired_at,
                sale_price_per_unit=sale_price,
                gain_native=c["gross_gain"].quantize(MONEY),
                fees_native=lot_fees,
                gain_inr=None,
                regime=holding.regime,
                holding_months=c["holding_months"],
                is_long_term=c["long_term"],
                days_to_long_term=c["days_to_long_term"],
                long_term_mark_date=c["long_term_mark_date"],
            )
        )

    gain_native = sum((c.gain_native for c in base_lots), ZERO).quantize(MONEY)
    ltcg_gain_native = sum((c.gain_native for c in base_lots if c.is_long_term), ZERO).quantize(
        MONEY
    )
    stcg_gain_native = gain_native - ltcg_gain_native

    # --- Price through the shared engine --------------------------------- #
    fy_label = fy_label_for_date(sale_date)
    month = sale_month_key(sale_date.date())

    def _synthetic_orders() -> list[OrderView]:
        orders: list[OrderView] = []
        for index, c in enumerate(base_lots):
            orders.append(
                OrderView(
                    public_id=f"sim-buy-{index:04d}-{c.lot_id or 'x'}",
                    order_type="buy",
                    symbol=holding.symbol,
                    quantity=c.quantity,
                    price_per_unit=c.cost_per_unit,
                    gross_amount=(c.quantity * c.cost_per_unit).quantize(MONEY),
                    brokerage_fee=ZERO,
                    tax_amount=ZERO,
                    other_fees=ZERO,
                    occurred_at=c.acquired_at,
                    currency=currency,
                    account_id=holding.account_id,
                    account_name=holding.account_name,
                    instrument_type="stock",
                )
            )
        orders.append(
            OrderView(
                public_id="sim-sell",
                order_type="sell",
                symbol=holding.symbol,
                quantity=to_consume,
                price_per_unit=sale_price,
                gross_amount=(to_consume * sale_price).quantize(MONEY),
                brokerage_fee=sell_fees,
                tax_amount=ZERO,
                other_fees=ZERO,
                occurred_at=sale_date,
                currency=currency,
                account_id=holding.account_id,
                account_name=holding.account_name,
                instrument_type="stock",
                # No recorded values: the reconciliation guard skips sells
                # without a recorded gain/cost.
                realized_gain_loss=None,
                avg_cost_at_sale=None,
            )
        )
        return orders

    estimate = compute_tax_estimate(
        orders=_synthetic_orders(),
        fy_label=fy_label,
        profile=profile,
        regime_resolver=lambda order: (
            regime_spec,
            holding.regime_source,
            f"{holding.regime.value} regime (holding snapshot)",
        ),
        app_fx_rates=app_fx_rates or {},
    )

    # --- FX rate used for this sale -------------------------------------- #
    fx_rate: Decimal | None = None
    fx_source = ""
    if currency == "INR":
        fx_rate, fx_source = Decimal("1"), "reporting_currency"
    else:
        matched = next(
            (f for f in estimate.fx_rates if f.month == month and f.currency.upper() == currency),
            None,
        )
        if matched is not None:
            fx_rate, fx_source = matched.rate, matched.source

    # --- Assemble the result --------------------------------------------- #
    final_lots: list[ConsumedLot] = []
    if estimate.status == "success" and fx_rate is not None:
        total_gain_inr = estimate.total_gain_inr
        total_tax_inr = estimate.total_tax_inr
        for c in base_lots:
            final_lots.append(
                ConsumedLot(
                    lot_id=c.lot_id,
                    quantity=c.quantity,
                    cost_per_unit=c.cost_per_unit,
                    acquired_at=c.acquired_at,
                    sale_price_per_unit=c.sale_price_per_unit,
                    gain_native=c.gain_native,
                    fees_native=c.fees_native,
                    gain_inr=(c.gain_native * fx_rate).quantize(MONEY),
                    regime=c.regime,
                    holding_months=c.holding_months,
                    is_long_term=c.is_long_term,
                    days_to_long_term=c.days_to_long_term,
                    long_term_mark_date=c.long_term_mark_date,
                )
            )
    else:
        total_gain_inr = None
        total_tax_inr = None
        final_lots = base_lots

    assumptions: list[str] = []
    if holding.regime == RegimeType.indian:
        assumptions.append(
            "Simulated in isolation — the full ₹1.25 lakh Sec 112A LTCG "
            "exemption is applied as if this sale were the FY's only "
            "long-term gain; existing FY LTCG would reduce the shelter."
        )
    assumptions.append("No prior-year losses applied.")
    if fx_rate is not None:
        specified = specified_fx_date(sale_date.date())
        assumptions.append(
            f"Rule 115 FX rate for {month}: ₹{fx_rate} on {specified} ({fx_source})."
        )
    else:
        assumptions.append(
            f"No Rule 115 exchange rate for {month} ({currency}); the tax "
            "figure cannot be computed until the rate is supplied."
        )

    if estimate.status == "missing_fx_rate":
        status = "missing_fx_rate"
        message = estimate.message
    elif insufficient:
        status = "insufficient_quantity"
        message = (
            f"Requested {qty} units but only {total_open} are open; "
            "simulating the maximum sellable quantity."
        )
    else:
        status = "success"
        message = None

    return SaleSimulation(
        holding_id=holding.holding_id,
        symbol=holding.symbol,
        regime=holding.regime,
        sale_date=sale_date,
        sale_price=sale_price,
        currency=currency,
        quantity_sold=qty,
        quantity_consumed=to_consume,
        status=status,
        consumed_lots=final_lots,
        gain_native=gain_native,
        ltcg_gain_native=ltcg_gain_native,
        stcg_gain_native=stcg_gain_native,
        gain_inr=total_gain_inr,
        tax_inr=total_tax_inr,
        fy_label=fy_label,
        fx_rate=fx_rate,
        fx_source=fx_source,
        assumptions=assumptions,
        message=message,
    )


# --------------------------------------------------------------------------- #
# list_tax_lots
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TaxLotView:
    """One open lot with its holding-period countdown."""

    lot_id: int | None
    quantity: Decimal
    cost_per_unit: Decimal
    acquired_at: datetime
    regime: RegimeType
    holding_months: int
    is_long_term: bool
    days_to_long_term: int
    long_term_mark_date: datetime
    unrealised_pnl_native: Decimal | None
    """``(current_price - cost) x quantity`` when a current price is given."""
    unrealised_pnl_inr: Decimal | None


def list_tax_lots(
    holding: HoldingSnapshot,
    *,
    as_of: datetime | None = None,
    current_price: Decimal | None = None,
    fx_rate: Decimal | None = None,
) -> list[TaxLotView]:
    """Open lots of a holding, oldest first, with holding-period countdown.

    ``days_to_long_term`` is 0 once the lot is long-term (12 months Indian,
    24 months foreign). ``as_of`` defaults to now; pass it explicitly for
    deterministic results.
    """
    if as_of is None:
        as_of = datetime.now(UTC)
    regime_spec = REGIME_SPECS[holding.regime]
    views: list[TaxLotView] = []
    for lot in sorted(holding.lots, key=lambda lot: (lot.acquired_at, str(lot.lot_id))):
        mark = add_months(lot.acquired_at, regime_spec.ltcg_holding_months)
        long_term = is_long_term(lot.acquired_at, as_of, regime_spec)
        delta = (mark.date() - as_of.date()).days
        days_to_lt = 0 if long_term else max(delta, 0)
        pnl_native: Decimal | None = None
        pnl_inr: Decimal | None = None
        if current_price is not None:
            pnl_native = ((current_price - lot.cost_per_unit) * lot.quantity).quantize(MONEY)
            if fx_rate is not None:
                pnl_inr = (pnl_native * fx_rate).quantize(MONEY)
        views.append(
            TaxLotView(
                lot_id=lot.lot_id,
                quantity=lot.quantity,
                cost_per_unit=lot.cost_per_unit,
                acquired_at=lot.acquired_at,
                regime=holding.regime,
                holding_months=months_between(lot.acquired_at, as_of),
                is_long_term=long_term,
                days_to_long_term=days_to_lt,
                long_term_mark_date=mark,
                unrealised_pnl_native=pnl_native,
                unrealised_pnl_inr=pnl_inr,
            )
        )
    return views


# --------------------------------------------------------------------------- #
# harvest_candidates
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HarvestCandidate:
    """An unrealised-loss lot, ranked by the tax selling it would save."""

    holding_id: int
    symbol: str
    lot_id: int | None
    quantity: Decimal
    cost_per_unit: Decimal
    current_price: Decimal
    unrealised_loss_native: Decimal
    """Positive magnitude of the unrealised loss in native currency."""
    regime: RegimeType
    is_long_term: bool
    rate_pct: Decimal | None
    """Tax rate at which the loss saves tax (None = slab-dependent)."""
    tax_saved_native: Decimal | None
    tax_saved_inr: Decimal | None
    reason: str


def harvest_candidates(
    holdings: list[HoldingSnapshot],
    current_prices: dict[int, Decimal],
    *,
    as_of: datetime | None = None,
    stcg_rate_pct: Decimal | None = None,
    fx_rates: dict[str, Decimal] | None = None,
) -> list[HarvestCandidate]:
    """Unrealised-loss lots across holdings, ranked by tax saved.

    A lot qualifies when its current price is below its cost basis. The
    saving is loss x rate: the regime's LTCG rate (12.5%) for long-term
    lots, the given STCG rate (or the regime's flat STCG rate, e.g. 20%
    under Sec 111A) otherwise.

    Holdings may be in different currencies, so ranking is by the saving
    in INR. ``fx_rates`` maps currency code to its INR rate (INR itself is
    always 1). Ranking tiers, best first: lots with a known INR saving
    (largest first); lots with a native saving but no rate for their
    currency (not comparable across currencies, so ranked after); lots
    whose rate is slab-dependent (foreign short-term), with
    ``rate_pct``/``tax_saved`` None, last.
    """
    if as_of is None:
        as_of = datetime.now(UTC)
    rates = {code.upper(): rate for code, rate in (fx_rates or {}).items()}
    rates["INR"] = Decimal("1")
    candidates: list[HarvestCandidate] = []
    for holding in holdings:
        price = current_prices.get(holding.holding_id)
        if price is None:
            continue
        regime_spec = REGIME_SPECS[holding.regime]
        for lot in holding.lots:
            loss = (lot.cost_per_unit - price) * lot.quantity
            if loss <= ZERO:
                continue
            loss = loss.quantize(MONEY)
            long_term = is_long_term(lot.acquired_at, as_of, regime_spec)
            if long_term:
                rate = regime_spec.ltcg_rate_pct
            elif stcg_rate_pct is not None:
                rate = stcg_rate_pct
            elif regime_spec.stcg_flat_rate_pct is not None:
                rate = regime_spec.stcg_flat_rate_pct
            else:
                rate = None
            tax_saved = (loss * rate).quantize(MONEY) if rate is not None else None
            fx_rate = rates.get(holding.currency.upper())
            tax_saved_inr = (
                (tax_saved * fx_rate).quantize(MONEY)
                if tax_saved is not None and fx_rate is not None
                else None
            )
            rate_label = _percent_label(rate)
            if long_term:
                reason = (
                    f"Long-term loss of {loss} at {rate_label} — realising it saves {tax_saved}."
                )
            elif rate is None:
                reason = (
                    f"Short-term loss of {loss} at the marginal slab rate — "
                    "tax saved depends on your total income (provide "
                    "stcg_rate_pct to rank)."
                )
            else:
                reason = (
                    f"Short-term loss of {loss} at {rate_label} — realising it saves {tax_saved}."
                )
            candidates.append(
                HarvestCandidate(
                    holding_id=holding.holding_id,
                    symbol=holding.symbol,
                    lot_id=lot.lot_id,
                    quantity=lot.quantity,
                    cost_per_unit=lot.cost_per_unit,
                    current_price=price,
                    unrealised_loss_native=loss,
                    regime=holding.regime,
                    is_long_term=long_term,
                    rate_pct=rate,
                    tax_saved_native=tax_saved,
                    tax_saved_inr=tax_saved_inr,
                    reason=reason,
                )
            )

    def _rank(c: HarvestCandidate) -> tuple[int, Decimal, Decimal]:
        if c.tax_saved_inr is not None:
            return (0, -c.tax_saved_inr, -c.unrealised_loss_native)
        if c.tax_saved_native is not None:
            return (1, -c.tax_saved_native, -c.unrealised_loss_native)
        return (2, ZERO, -c.unrealised_loss_native)

    candidates.sort(key=_rank)
    return candidates
