"""The tax estimate orchestrator.

Ties the pieces together: replay the orders, classify each lot's regime,
convert to INR at the Rule 115 rate, bucket the gains, resolve the slab,
and compute the tax.

The reconciliation guard is the important property here. Every sell order
records ``realized_gain_loss`` and ``avg_cost_at_sale``; if the FIFO
replay does not reproduce them, the engine returns
``status="reconciliation_failed"`` and **no tax number**. A tax figure
that has not passed this check must not be presented.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.investing.tax.fy import fy_bounds
from app.investing.tax.gains import (
    OrderView,
    ReplayResult,
    TaxLot,
    replay_fifo_lots,
)
from app.investing.tax.regime import FOREIGN_REGIME, INDIAN_REGIME, RegimeSpec, RegimeType
from app.investing.tax.slab import (
    SlabResolution,
    capital_gains_tax,
    compute_rebate,
    derive_slab,
)
from app.investing.tax.tax_profile import FxRate, TaxProfile

MONEY = Decimal("0.01")
REPORTING_CURRENCY = "INR"

REGIME_SPECS: dict[RegimeType, RegimeSpec] = {
    RegimeType.indian: INDIAN_REGIME,
    RegimeType.foreign: FOREIGN_REGIME,
}


@dataclass
class BucketResult:
    """One bucket's totals and tax."""

    regime: RegimeType
    section: str
    rate_pct: Decimal
    gain_native: Decimal = Decimal("0")
    gain_inr: Decimal = Decimal("0")
    fees_native: Decimal = Decimal("0")
    fees_inr: Decimal = Decimal("0")
    exemption_applied: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    sell_count: int = 0
    lot_count: int = 0

    def as_dict(self) -> dict[str, str | int]:
        return {
            "regime": str(self.regime),
            "section": self.section,
            "rate_pct": str(self.rate_pct),
            "gain_native": str(self.gain_native),
            "gain_inr": str(self.gain_inr),
            "fees_native": str(self.fees_native),
            "fees_inr": str(self.fees_inr),
            "exemption_applied": str(self.exemption_applied),
            "tax": str(self.tax),
            "sell_count": self.sell_count,
            "lot_count": self.lot_count,
        }


@dataclass
class TaxEstimate:
    """The full result of a tax computation."""

    status: str
    fy_label: str
    currency: str = "INR"
    buckets: dict[str, BucketResult] = field(default_factory=dict)
    total_gain_inr: Decimal = Decimal("0")
    total_tax_inr: Decimal = Decimal("0")
    effective_rate_pct: Decimal = Decimal("0")
    slab: SlabResolution | None = None
    fx_rates: list[FxRate] = field(default_factory=list)
    lots: list[TaxLot] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    disclaimers: list[str] = field(default_factory=list)
    reconciliation_failures: list[dict[str, str]] = field(default_factory=list)
    missing_fx_rates: list[dict[str, str]] = field(default_factory=list)
    """(month, currency) pairs that need an FX rate but have none."""
    message: str | None = None


def specified_fx_date(sale_date: date) -> date:
    """The Rule 115 specified date: last day of the month before the sale.

    Capital gains use the SBI TT buying rate on the last day of the month
    preceding the month of transfer.
    """
    if sale_date.month == 1:
        year, month = sale_date.year - 1, 12
    else:
        year, month = sale_date.year, sale_date.month - 1
    return date(year, month, calendar.monthrange(year, month)[1])


def sale_month_key(sale_date: date) -> str:
    return f"{sale_date.year:04d}-{sale_date.month:02d}"


def _parse_override_key(key: str) -> tuple[date | None, str | None]:
    """Return (date, currency) for daily override keys; legacy month keys return (None, None)."""
    date_part, separator, currency = key.partition(":")
    try:
        parsed = date.fromisoformat(date_part)
    except ValueError:
        return None, None
    if separator:
        if len(currency) != 3 or not currency.isalpha():
            return None, None
        return parsed, currency.upper()
    return parsed, None


def resolve_fx_rate(
    *,
    specified_date: date,
    sale_month: str,
    currency: str,
    fx_overrides: dict[str, Decimal],
    app_fx_rates: dict[tuple[str, str], Decimal],
) -> tuple[Decimal | None, str, bool]:
    """Resolve manual daily/legacy FX overrides before app estimates.

    Exact date+currency wins, then date-only, then nearest same-currency
    daily rate within 35 days, legacy sale-month override, and app estimate.
    """
    currency = currency.upper()
    exact_key = f"{specified_date.isoformat()}:{currency}"
    if exact_key in fx_overrides:
        return fx_overrides[exact_key], "manual", False
    date_key = specified_date.isoformat()
    if date_key in fx_overrides:
        return fx_overrides[date_key], "manual", False

    candidates: list[tuple[int, str, Decimal]] = []
    for key, rate in fx_overrides.items():
        override_date, override_currency = _parse_override_key(key)
        if override_date is None or override_currency != currency:
            continue
        distance = abs((override_date - specified_date).days)
        if distance <= 35:
            candidates.append((distance, override_date.isoformat(), rate))
    if candidates:
        _distance, matched_date, rate = min(candidates)
        return rate, f"manual_nearest:{matched_date}", False

    if sale_month in fx_overrides:
        return fx_overrides[sale_month], "manual", False
    app_rate = app_fx_rates.get((sale_month, currency))
    if app_rate is not None:
        return app_rate, "app_rate_estimate", True
    return None, "missing", False


def compute_tax_estimate(
    *,
    orders: list[OrderView],
    fy_label: str,
    profile: TaxProfile,
    regime_resolver,
    app_fx_rates: dict[tuple[str, str], Decimal] | None = None,
) -> TaxEstimate:
    """Compute the capital gains tax estimate for one FY.

    ``regime_resolver(order)`` returns ``(regime_spec, classified_by,
    classification_detail)`` for a sell order.

    ``app_fx_rates`` maps ``(month, currency)`` to the INR rate for that
    sale month. The currency is part of the key because a USD sale and a
    GBP sale in the same month need different rates.
    """
    start, end = fy_bounds(fy_label)
    app_fx_rates = app_fx_rates or {}

    by_symbol: dict[tuple[str, str], list[OrderView]] = defaultdict(list)
    for order in orders:
        by_symbol[(order.account_id, order.symbol)].append(order)

    all_lots: list[TaxLot] = []
    reconciliations: list[ReplayResult] = []
    for group in by_symbol.values():
        replay = replay_fifo_lots(
            group,
            regime_resolver=regime_resolver,
            fy_start=start,
            fy_end=end,
        )
        all_lots.extend(replay.lots)
        reconciliations.append(replay)

    estimate = TaxEstimate(status="success", fy_label=fy_label)

    failures = [
        {
            "symbol": r.symbol,
            "public_id": r.public_id,
            "expected_gain": str(r.recorded_gain),
            "computed_gain": str(r.computed_gain),
            "expected_cost": str(r.recorded_cost_per_unit),
            "computed_cost": str(r.computed_cost_per_unit),
        }
        for replay in reconciliations
        for r in replay.failures
    ]
    total_sells = sum(len(r.reconciliations) for r in reconciliations)
    if failures:
        estimate.status = "reconciliation_failed"
        estimate.reconciliation_failures = failures
        estimate.message = (
            f"FIFO replay does not reproduce the recorded gain for "
            f"{len(failures)} of {total_sells} sells. No tax figure is shown "
            "because the underlying data does not reconcile."
        )
        return estimate

    # --- FX rates per (sale month, currency) ---
    # A sale already denominated in INR needs no conversion: its rate is
    # 1, but it still has a specified date so the FX table is complete.
    fx_by_month: dict[tuple[str, str], FxRate] = {}
    missing: set[tuple[str, str]] = set()
    for lot in all_lots:
        month = sale_month_key(lot.sold_at.date())
        currency = lot.currency.upper()
        key = (month, currency)
        if key in fx_by_month:
            continue
        specified = specified_fx_date(lot.sold_at.date())
        if currency == REPORTING_CURRENCY:
            fx_by_month[key] = FxRate(
                month=month,
                specified_date=specified.isoformat(),
                currency=currency,
                rate=Decimal("1"),
                source="reporting_currency",
                is_estimated=False,
            )
            continue
        rate, source, is_estimated = resolve_fx_rate(
            specified_date=specified,
            sale_month=month,
            currency=currency,
            fx_overrides=profile.fx_overrides,
            app_fx_rates=app_fx_rates,
        )
        if rate is None:
            missing.add((month, currency))
            continue
        fx_by_month[key] = FxRate(
            month=month,
            specified_date=specified.isoformat(),
            currency=currency,
            rate=rate,
            source=source,
            is_estimated=is_estimated,
        )
    estimate.fx_rates = sorted(fx_by_month.values(), key=lambda f: (f.month, f.currency))
    estimate.missing_fx_rates = [
        {"month": month, "currency": currency} for month, currency in sorted(missing)
    ]
    if estimate.missing_fx_rates:
        # Some foreign-currency lots have no Rule 115 rate. Converting them
        # would need a rate we do not have, and omitting them would
        # understate the tax. Present neither a number nor a silent gap.
        estimate.status = "missing_fx_rate"
        detail = ", ".join(f"{m['month']} ({m['currency']})" for m in estimate.missing_fx_rates)
        estimate.message = (
            "No Rule 115 exchange rate is available for: "
            f"{detail}. Supply the rate for those months to compute the tax "
            "estimate."
        )
        return estimate

    # --- Bucket the lots ---
    buckets: dict[tuple[RegimeType, str], BucketResult] = {}
    for lot in all_lots:
        regime_spec = REGIME_SPECS[lot.regime]
        term = "long_term" if lot.is_long_term else "short_term"
        key = (lot.regime, term)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = BucketResult(
                regime=lot.regime,
                section=(
                    regime_spec.ltcg_section if lot.is_long_term else regime_spec.stcg_section
                ),
                rate_pct=(
                    regime_spec.ltcg_rate_pct
                    if lot.is_long_term
                    else (regime_spec.stcg_flat_rate_pct or Decimal("0"))
                ),
            )
            buckets[key] = bucket
        bucket.gain_native += lot.gain_native
        bucket.fees_native += lot.fees_native
        bucket.lot_count += 1

    # --- Convert to INR, deducting each lot's pro-rata fees ---
    for (regime_key, term), bucket in buckets.items():
        gain_inr = Decimal("0")
        fees_inr = Decimal("0")
        for lot in all_lots:
            if lot.regime != regime_key:
                continue
            if ("long_term" if lot.is_long_term else "short_term") != term:
                continue
            rate = fx_by_month.get((sale_month_key(lot.sold_at.date()), lot.currency.upper()))
            if rate is None:
                continue
            gain_inr += lot.gain_native * rate.rate
            fees_inr += lot.fees_native * rate.rate
            lot.gain_inr = (lot.gain_native * rate.rate).quantize(MONEY)
        bucket.gain_inr = gain_inr.quantize(MONEY)
        bucket.fees_inr = fees_inr.quantize(MONEY)
        bucket.gain_inr = (bucket.gain_inr - bucket.fees_inr).quantize(MONEY)

    # Count distinct sells per bucket.
    sells_by_bucket: dict[tuple[RegimeType, str], set[tuple[str, object]]] = defaultdict(set)
    for lot in all_lots:
        term = "long_term" if lot.is_long_term else "short_term"
        sells_by_bucket[(lot.regime, term)].add((lot.symbol, lot.sold_at))
    for key, bucket in buckets.items():
        bucket.sell_count = len(sells_by_bucket.get(key, set()))

    # --- Resolve the slab ---
    # The marginal rate applies to the short-term gain, but the *total income*
    # that sets the band, the surcharge and the 87A rebate ceiling includes
    # every capital gain, long-term included.
    all_gains_inr = sum((b.gain_inr for b in buckets.values()), Decimal("0"))
    slab = derive_slab(
        other_income=profile.other_income,
        capital_gains_income=all_gains_inr,
        tax_regime=profile.tax_regime,
        slab_override_pct=profile.slab_override_pct,
        surcharge_override_pct=profile.surcharge_override_pct,
        cess_pct=profile.cess_pct,
    )
    estimate.slab = slab

    # --- Exemption and tax ---
    for (regime_key, term), bucket in buckets.items():
        regime_spec = REGIME_SPECS[regime_key]
        if term == "long_term":
            exemption = regime_spec.ltcg_exemption_inr
            bucket.exemption_applied = (
                min(exemption, bucket.gain_inr) if bucket.gain_inr > 0 else Decimal("0")
            )
            taxable = max(Decimal("0"), bucket.gain_inr - exemption)
            bucket.tax = capital_gains_tax(
                taxable,
                rate=regime_spec.ltcg_rate_pct,
                regime_cess=regime_spec.cess_pct,
                slab=slab,
            )
        elif regime_spec.stcg_is_flat and regime_spec.stcg_flat_rate_pct is not None:
            bucket.tax = capital_gains_tax(
                bucket.gain_inr,
                rate=regime_spec.stcg_flat_rate_pct,
                regime_cess=regime_spec.cess_pct,
                slab=slab,
            )
        else:
            bucket.tax = capital_gains_tax(
                bucket.gain_inr,
                rate=slab.rate_pct,
                regime_cess=regime_spec.cess_pct,
                slab=slab,
            )

    estimate.buckets = {
        f"{regime_key}:{term}": bucket for (regime_key, term), bucket in buckets.items()
    }
    estimate.total_gain_inr = sum((b.gain_inr for b in buckets.values()), Decimal("0")).quantize(
        MONEY
    )
    gross_tax = sum((b.tax for b in buckets.values()), Decimal("0")).quantize(MONEY)
    rebate = compute_rebate(
        gross_tax,
        slab.total_income_considered,
        tax_regime=profile.tax_regime,
    )
    estimate.total_tax_inr = (gross_tax - rebate).quantize(MONEY)
    slab.rebate_amount = rebate
    if rebate:
        estimate.assumptions.append(
            f"Section 87A rebate of ₹{rebate:,.0f} applied against the total "
            f"tax (taxable income ≤ ₹12,00,000, {profile.tax_regime} regime)."
        )
    estimate.effective_rate_pct = (
        (estimate.total_tax_inr / estimate.total_gain_inr * Decimal("100"))
        if estimate.total_gain_inr > 0
        else Decimal("0")
    ).quantize(Decimal("0.01"))
    estimate.lots = all_lots
    estimate.assumptions = _build_assumptions(estimate, buckets, fx_by_month)
    estimate.disclaimers = [
        "This is an estimate, not a filing-ready liability. Verify the slab, "
        "the Rule 115 FX rate and the regime with your CA before filing.",
        "Short-term gains are taxed at your marginal rate on total income, "
        "not at a standalone rate. A full return computation is out of scope.",
    ]
    return estimate


def _build_assumptions(
    estimate: TaxEstimate,
    buckets: dict,
    fx_by_month: dict[str, FxRate],
) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for regime_key, _term in buckets:
        if str(regime_key) in seen:
            continue
        seen.add(str(regime_key))
        spec = REGIME_SPECS[regime_key]
        lines.append(
            f"Regime '{regime_key}' ({spec.label}) — {spec.ltcg_holding_months}-month "
            f"long-term threshold, {spec.ltcg_section} for long-term gains and "
            f"{spec.stcg_section} for short-term gains."
        )
    if estimate.slab is not None:
        slab = estimate.slab
        lines.append(
            f"Short-term gains taxed at {slab.rate_pct}% "
            f"({'as entered' if slab.source == 'override' else 'derived from taxable other income'}) "
            f"under the {slab.tax_regime} tax regime."
        )
        lines.extend(slab.caveats)
    manual = [
        f
        for f in fx_by_month.values()
        if not f.is_estimated and not f.source.startswith("manual_nearest:")
    ]
    nearest = [f for f in fx_by_month.values() if f.source.startswith("manual_nearest:")]
    estimated = [f for f in fx_by_month.values() if f.is_estimated]
    if manual:
        lines.append(
            "Rule 115 FX rate supplied manually for "
            + ", ".join(f"{f.month} (₹{f.rate} on {f.specified_date})" for f in manual)
            + "."
        )
    if nearest:
        lines.append(
            "Rule 115 FX rate resolved from the nearest entered date for "
            + ", ".join(
                f"{f.month} (₹{f.rate} from {f.source.split(':', 1)[1]}, specified {f.specified_date})"
                for f in nearest
            )
            + "."
        )
    if estimated:
        lines.append(
            "Rule 115 FX rate estimated from the app's stored rate for "
            + ", ".join(f"{f.month} ({f.specified_date})" for f in estimated)
            + " — verify against SBI's published TT buying rate."
        )
    lines.append("No prior-year losses applied.")
    return lines
