"""Read-only capital-gains tax what-if simulations (spec-099 extension).

``simulate_sale``, ``list_tax_lots`` and ``harvest_candidates`` are pure:
they take plain-data snapshots and return plain data. They never open a DB
session and never mutate a holding — they answer "what if I sell N units
of X on date D at price P?" for the user.

The tax numbers reuse the same engine used for the real estimate
(``compute_tax_estimate``), so a simulation and a filing apply identical
statutory rules (FIFO cost basis, exact 12/24-month holding thresholds,
Rule 115 FX, slab/surcharge/cess/rebate).

Tax is reported **incrementally**. The caller passes the FY's already
realised lots (``realised_lots``, typically ``TaxEstimate.lots``); the
engine prices the FY once without and once with the simulated sale, so the
Sec 112A exemption only shelters what is *left* after gains already booked,
and the slab/surcharge/rebate see the whole FY. The difference is the tax
caused by the sale. No tax figure is presented when the Rule 115 FX rate is
unavailable (mirroring the engine's reconciliation guard).

All holding-period arithmetic is by **calendar day**: timestamps are
collapsed to the date they carry in their own timezone, so a caller that
converts to the workspace-local day gets local-day semantics.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.investing.tax.engine import (
    REPORTING_CURRENCY,
    TaxEstimate,
    compute_tax_estimate,
    resolve_fx_rate,
    sale_month_key,
    specified_fx_date,
)
from app.investing.tax.fy import add_months, fy_label_for_date, months_between
from app.investing.tax.gains import OrderView, TaxLot, order_total_fees
from app.investing.tax.regime import REGIMES as REGIME_SPECS
from app.investing.tax.regime import RegimeSpec, RegimeType
from app.investing.tax.tax_profile import TaxProfile

MONEY = Decimal("0.01")
ZERO = Decimal("0")


def _as_day(value: datetime) -> datetime:
    """Collapse a timezone-aware timestamp to its calendar day.

    Returns midnight of the date the timestamp carries *in its own
    timezone*, as a UTC-tagged carrier so every derived value is a plain
    comparable datetime. Naive datetimes are rejected: comparing naive and
    aware values raises ``TypeError`` deep in the engine, and silently
    assuming a zone would shift the tax year / FX month near midnight.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetimes must be timezone-aware")
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


@dataclass(frozen=True)
class _HoldingPeriod:
    is_long_term: bool
    holding_months: int
    days_to_long_term: int
    mark_date: datetime
    long_term_from: date


def _holding_period(acquired_at: datetime, as_of: datetime, spec: RegimeSpec) -> _HoldingPeriod:
    """Classify one lot on a calendar-day basis.

    "More than N months" means a sale is long-term only on the day *after*
    the N-month anniversary, so ``days_to_long_term`` counts to that first
    long-term day and is 0 only once the lot is already long-term.
    """
    acquired_day = _as_day(acquired_at)
    as_of_day = _as_day(as_of)
    mark = add_months(acquired_day, spec.ltcg_holding_months)
    first_long_term = (mark + timedelta(days=1)).date()
    long_term = as_of_day > mark
    return _HoldingPeriod(
        is_long_term=long_term,
        holding_months=months_between(acquired_day, as_of_day),
        days_to_long_term=0 if long_term else (first_long_term - as_of_day.date()).days,
        mark_date=mark,
        long_term_from=first_long_term,
    )


def _lot_order(lot: LotSnapshot) -> tuple[datetime, bool, int]:
    # FIFO: acquisition time first, then the numeric lot id (string ids
    # would sort "10" before "2"). Synthetic lots (id None) sort last.
    return (lot.acquired_at, lot.lot_id is None, lot.lot_id or 0)


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
    """Calendar days from the sale date to the first day a sale of this lot
    is long-term (0 = already long-term on the sale date)."""
    long_term_mark_date: datetime
    """The N-month anniversary of acquisition; a sale must fall *after* it."""
    long_term_from: date | None = None
    """First calendar date on which a sale of this lot is long-term."""


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
    """Incremental tax caused by this sale: FY tax with the sale minus FY tax
    without it (negative when a loss cuts tax on gains already realised)."""
    tax_before_inr: Decimal | None = None
    """Estimated FY tax on the lots already realised, before this sale."""
    tax_after_inr: Decimal | None = None
    """Estimated FY tax including this sale."""
    fy_label: str = ""
    fy_realised_lots: int = 0
    """How many already-realised lots of the FY were taken into account."""
    fy_realised_gain_inr: Decimal | None = None
    """Net INR gain of those lots (after sell fees)."""
    fy_realised_ltcg_inr: Decimal | None = None
    """Net Indian-regime (Sec 112A) LTCG already booked in the FY."""
    ltcg_exemption_remaining_inr: Decimal | None = None
    """Sec 112A exemption still unused before this sale (Indian regime)."""
    ltcg_exemption_used_inr: Decimal | None = None
    """Part of that exemption this sale absorbs (Indian regime)."""
    fx_rate: Decimal | None = None
    fx_source: str = ""
    assumptions: list[str] = field(default_factory=list)
    message: str | None = None


def _bucket_gain(estimate: TaxEstimate | None, regime: RegimeType, term: str) -> Decimal:
    if estimate is None:
        return ZERO
    bucket = estimate.buckets.get(f"{regime}:{term}")
    return bucket.gain_inr if bucket is not None else ZERO


def _bucket_exemption(estimate: TaxEstimate | None, regime: RegimeType) -> Decimal:
    if estimate is None:
        return ZERO
    bucket = estimate.buckets.get(f"{regime}:long_term")
    return bucket.exemption_applied if bucket is not None else ZERO


def simulate_sale(
    holding: HoldingSnapshot,
    *,
    qty: Decimal,
    sale_date: datetime,
    sale_price: Decimal,
    profile: TaxProfile,
    app_fx_rates: dict[tuple[str, str], Decimal] | None = None,
    sell_fees: Decimal = ZERO,
    realised_lots: Sequence[TaxLot] = (),
) -> SaleSimulation:
    """Simulate selling ``qty`` units at ``sale_price`` on ``sale_date``.

    Consumes the holding's open lots FIFO (oldest first), classifies each
    consumed chunk long-term/short-term under the holding's regime, and
    prices the result through :func:`compute_tax_estimate` using synthetic
    buy + sell OrderViews — so the LTCG/STCG split, the Rule 115 FX rate
    and its source, and the estimated tax all come from the same engine
    the real estimate uses.

    ``realised_lots`` are the lots already realised in the sale's financial
    year (``TaxEstimate.lots`` of the real estimate for that FY). The FY is
    priced without and with the simulated sale and the difference is
    reported as ``tax_inr``, so the Sec 112A exemption only covers what is
    left after those lots. Lots outside the sale's FY are ignored.

    Lots acquired after ``sale_date`` are not sellable on that date and are
    excluded (noted in the assumptions).
    """
    if not qty.is_finite() or qty <= 0:
        raise ValueError("qty must be positive")
    if not sale_price.is_finite() or sale_price < 0:
        raise ValueError("sale_price must not be negative")
    if not sell_fees.is_finite() or sell_fees < 0:
        raise ValueError("sell_fees must not be negative")
    sale_day = _as_day(sale_date)

    regime_spec: RegimeSpec = REGIME_SPECS[holding.regime]
    currency = holding.currency.upper()

    # Open lots sellable on the sale date, oldest acquisition first.
    positive = [lot for lot in holding.lots if lot.quantity > 0]
    lots = sorted(
        (lot for lot in positive if _as_day(lot.acquired_at) <= sale_day),
        key=_lot_order,
    )
    lots_after_sale = len(positive) - len(lots)
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
        period = _holding_period(lot.acquired_at, sale_day, regime_spec)
        chunks.append({
            "lot": lot,
            "take": take,
            "gross_gain": take * (sale_price - lot.cost_per_unit),
            "period": period,
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
            occurred_at=sale_day,
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
        period: _HoldingPeriod = c["period"]
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
                holding_months=period.holding_months,
                is_long_term=period.is_long_term,
                days_to_long_term=period.days_to_long_term,
                long_term_mark_date=period.mark_date,
                long_term_from=period.long_term_from,
            )
        )

    gain_native = sum((c.gain_native for c in base_lots), ZERO).quantize(MONEY)
    ltcg_gain_native = sum((c.gain_native for c in base_lots if c.is_long_term), ZERO).quantize(
        MONEY
    )
    stcg_gain_native = gain_native - ltcg_gain_native

    # --- FX rate used for this sale (the engine's own resolver) ----------- #
    fy_label = fy_label_for_date(sale_day)
    month = sale_month_key(sale_day.date())
    fx_rate: Decimal | None
    if currency == REPORTING_CURRENCY:
        fx_rate, fx_source = Decimal("1"), "reporting_currency"
    else:
        fx_rate, fx_source, _estimated = resolve_fx_rate(
            specified_date=specified_fx_date(sale_day.date()),
            sale_month=month,
            currency=currency,
            fx_overrides=profile.fx_overrides,
            app_fx_rates=app_fx_rates or {},
        )
        if fx_rate is None:
            fx_source = ""

    # --- Price through the shared engine --------------------------------- #
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
                    occurred_at=_as_day(c.acquired_at),
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
                occurred_at=sale_day,
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

    def _price(orders: list[OrderView]) -> TaxEstimate:
        return compute_tax_estimate(
            orders=orders,
            fy_label=fy_label,
            profile=profile,
            regime_resolver=lambda order: (
                regime_spec,
                holding.regime_source,
                f"{holding.regime.value} regime (holding snapshot)",
            ),
            app_fx_rates=app_fx_rates or {},
            realised_lots=realised_lots,
        )

    before: TaxEstimate | None = None
    after: TaxEstimate | None = None
    if base_lots:
        after = _price(_synthetic_orders())
        if after.status == "success":
            before = _price([])

    priced = (
        after is not None
        and before is not None
        and after.status == "success"
        and before.status == "success"
        and fx_rate is not None
    )

    # --- Assemble the result --------------------------------------------- #
    final_lots: list[ConsumedLot] = []
    total_gain_inr: Decimal | None = None
    total_tax_inr: Decimal | None = None
    tax_before: Decimal | None = None
    tax_after: Decimal | None = None
    if priced:
        assert after is not None and before is not None and fx_rate is not None
        total_gain_inr = (after.total_gain_inr - before.total_gain_inr).quantize(MONEY)
        tax_before = before.total_tax_inr
        tax_after = after.total_tax_inr
        total_tax_inr = (tax_after - tax_before).quantize(MONEY)
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
                    long_term_from=c.long_term_from,
                )
            )
    else:
        final_lots = base_lots

    realised_in_fy = before if before is not None else None
    realised_count = (
        len(realised_in_fy.lots) if realised_in_fy is not None and realised_in_fy.lots else 0
    )
    realised_gain: Decimal | None = realised_in_fy.total_gain_inr if priced else None
    realised_ltcg: Decimal | None = None
    exemption_remaining: Decimal | None = None
    exemption_used: Decimal | None = None
    if priced and holding.regime == RegimeType.indian:
        cap = regime_spec.ltcg_exemption_inr
        realised_ltcg = _bucket_gain(before, RegimeType.indian, "long_term")
        exemption_remaining = max(ZERO, cap - max(ZERO, realised_ltcg))
        exemption_used = _bucket_exemption(after, RegimeType.indian) - _bucket_exemption(
            before, RegimeType.indian
        )

    assumptions: list[str] = []
    if priced:
        assumptions.append(
            f"Tax shown is incremental: estimated {fy_label} tax with this sale minus "
            f"without it, over {realised_count} lot(s) already realised in that FY "
            f"(net gain ₹{realised_gain})."
        )
    if priced and holding.regime == RegimeType.indian:
        assumptions.append(
            f"Sec 112A exemption: ₹{realised_ltcg} of long-term gain is already booked in "
            f"{fy_label}, leaving ₹{exemption_remaining} of the ₹{regime_spec.ltcg_exemption_inr} "
            f"exemption; this sale uses ₹{exemption_used}."
        )
    assumptions.append("No prior-year losses applied.")
    if priced and gain_native < 0:
        assumptions.append(
            "A loss is netted only within its own regime/term bucket; cross-bucket "
            "set-off (e.g. short-term loss against long-term gain) is not modelled."
        )
    if fx_rate is not None:
        specified = specified_fx_date(sale_day.date())
        assumptions.append(
            f"Rule 115 FX rate for {month}: ₹{fx_rate} on {specified} ({fx_source})."
            + (
                " This is the app's stored market rate — verify against SBI's TT buying rate."
                if fx_source == "app_rate_estimate"
                else ""
            )
        )
    else:
        assumptions.append(
            f"No Rule 115 exchange rate for {month} ({currency}); the tax "
            "figure cannot be computed until the rate is supplied."
        )
    if lots_after_sale:
        assumptions.append(
            f"{lots_after_sale} open lot(s) acquired after the sale date were excluded "
            "(they cannot be sold on that date)."
        )
    assumptions.append("Open lots are as currently recorded; later real trades are not replayed.")

    if after is not None and after.status != "success":
        status = after.status
        message = after.message
    elif before is not None and before.status != "success":
        status = before.status
        message = before.message
    elif fx_rate is None:
        status = "missing_fx_rate"
        message = (
            f"No Rule 115 exchange rate is available for: {month} ({currency}). "
            "Supply the rate for that month to compute the tax estimate."
        )
    elif insufficient:
        status = "insufficient_quantity"
        message = (
            f"Requested {qty} units but only {total_open} are open"
            f"{' on the sale date' if lots_after_sale else ''}; "
            "simulating the maximum sellable quantity."
            if total_open > 0
            else f"Requested {qty} units but none are open on the sale date."
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
        tax_before_inr=tax_before,
        tax_after_inr=tax_after,
        fy_label=fy_label,
        fy_realised_lots=realised_count,
        fy_realised_gain_inr=realised_gain,
        fy_realised_ltcg_inr=realised_ltcg,
        ltcg_exemption_remaining_inr=exemption_remaining,
        ltcg_exemption_used_inr=exemption_used,
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
    long_term_from: date
    """First calendar date on which a sale of this lot is long-term."""
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
    24 months foreign); before that it counts the calendar days to the first
    long-term day (the day after the anniversary, since the holding must
    exceed the threshold). ``as_of`` defaults to now; pass it explicitly
    for deterministic results.
    """
    if as_of is None:
        as_of = datetime.now(UTC)
    regime_spec = REGIME_SPECS[holding.regime]
    views: list[TaxLotView] = []
    for lot in sorted((lot for lot in holding.lots if lot.quantity > 0), key=_lot_order):
        period = _holding_period(lot.acquired_at, as_of, regime_spec)
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
                holding_months=period.holding_months,
                is_long_term=period.is_long_term,
                days_to_long_term=period.days_to_long_term,
                long_term_mark_date=period.mark_date,
                long_term_from=period.long_term_from,
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
            if lot.quantity <= 0:
                continue
            loss = (lot.cost_per_unit - price) * lot.quantity
            if loss <= ZERO:
                continue
            loss = loss.quantize(MONEY)
            long_term = _holding_period(lot.acquired_at, as_of, regime_spec).is_long_term
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
