"""Slab, surcharge, cess and rebate resolution.

A slab is a *marginal* rate, so it cannot simply be stored: short-term
capital gains are added to the taxpayer's other income and the combined
total sets the rate. This module derives the rate from other income,
applies surcharge (with the capital-gains surcharge cap and marginal
relief), then cess, then the 87A rebate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

# Regime selector values.
TAX_REGIME_NEW = "new"
TAX_REGIME_OLD = "old"

# (upper bound inclusive, rate) — None means "and above".
NEW_REGIME_SLABS: list[tuple[Decimal | None, Decimal]] = [
    (Decimal("400000"), Decimal("0")),
    (Decimal("800000"), Decimal("0.05")),
    (Decimal("1200000"), Decimal("0.10")),
    (Decimal("1600000"), Decimal("0.15")),
    (Decimal("2000000"), Decimal("0.20")),
    (Decimal("2400000"), Decimal("0.25")),
    (None, Decimal("0.30")),
]

OLD_REGIME_SLABS: list[tuple[Decimal | None, Decimal]] = [
    (Decimal("250000"), Decimal("0")),
    (Decimal("500000"), Decimal("0.05")),
    (Decimal("1000000"), Decimal("0.20")),
    (None, Decimal("0.30")),
]

SURCHARGE_BANDS: list[tuple[Decimal, Decimal]] = [
    (Decimal("5000000"), Decimal("0.10")),
    (Decimal("10000000"), Decimal("0.15")),
    (Decimal("20000000"), Decimal("0.25")),
    (Decimal("50000000"), Decimal("0.37")),
]

# The 25% and 37% surcharge rates are not levied on income chargeable
# under Sec 111A / 112 / 112A, so capital-gains income sees a 15% cap.
CAPITAL_GAINS_SURCHARGE_CAP = Decimal("0.15")

CESS_RATE = Decimal("0.04")

REBATE_NEW_LIMIT = Decimal("60000")
REBATE_OLD_LIMIT = Decimal("12000")
REBATE_INCOME_CEILING = Decimal("1200000")


@dataclass
class SlabResolution:
    """The resolved rate and its components."""

    rate_pct: Decimal
    source: str
    """``derived`` or ``override``."""

    tax_regime: str
    surcharge_pct: Decimal
    surcharge_source: str
    cess_pct: Decimal
    rebate_amount: Decimal
    total_income_considered: Decimal
    marginal_relief_applied: bool = False
    caveats: list[str] = field(default_factory=list)


def slabs_for_regime(tax_regime: str) -> list[tuple[Decimal | None, Decimal]]:
    if tax_regime == TAX_REGIME_OLD:
        return OLD_REGIME_SLABS
    return NEW_REGIME_SLABS


def tax_on_income(total_income: Decimal, slabs: list[tuple[Decimal | None, Decimal]]) -> Decimal:
    """Tax on total income under a slab table (no surcharge, no cess)."""
    tax = Decimal("0")
    previous = Decimal("0")
    for cap, rate in slabs:
        cap_value = Decimal("99999999999999") if cap is None else cap
        if total_income <= previous:
            break
        portion = min(total_income, cap_value) - previous
        tax += portion * rate
        previous = cap_value
        if cap is not None and total_income <= cap_value:
            break
    return tax


def marginal_rate(total_income: Decimal, slabs: list[tuple[Decimal | None, Decimal]]) -> Decimal:
    """The rate applied to the topmost slab of ``total_income``."""
    rate = Decimal("0")
    for cap, slab_rate in slabs:
        cap_value = Decimal("99999999999999") if cap is None else cap
        rate = slab_rate
        if total_income <= cap_value:
            break
    return rate


def surcharge_rate(
    total_income: Decimal,
    *,
    capital_gains_only: bool = False,
    tax_regime: str = TAX_REGIME_NEW,
) -> Decimal:
    """Surcharge rate on the tax, with marginal relief applied.

    Marginal relief caps the tax-plus-surcharge so it cannot exceed the
    tax on the band threshold by more than the income exceeding that
    threshold. That is why the rate at ₹50,00,001 is near zero rather
    than a full 10% — the extra rupee of income cannot attract a full
    surcharge.
    """
    rate = Decimal("0")
    threshold = Decimal("0")
    for cap, band_rate in SURCHARGE_BANDS:
        if total_income > cap:
            rate = band_rate
            threshold = cap
        else:
            break
    if rate == 0:
        return rate

    if capital_gains_only:
        # Sec 111A/112/112A income sees the 15% cap regardless of band.
        # Marginal relief is a property of the whole-income computation,
        # which this estimate does not perform, so it is not applied here.
        return min(rate, CAPITAL_GAINS_SURCHARGE_CAP)

    slabs = slabs_for_regime(tax_regime)
    base_tax = tax_on_income(threshold, slabs)
    excess = total_income - threshold
    ceiling = base_tax + excess
    tax_at_income = tax_on_income(total_income, slabs)
    if tax_at_income + tax_at_income * rate > ceiling:
        if tax_at_income == 0:
            return Decimal("0")
        return max(Decimal("0"), (ceiling - tax_at_income) / tax_at_income)
    return rate


def rebate_limit(tax_regime: str) -> Decimal:
    return REBATE_OLD_LIMIT if tax_regime == TAX_REGIME_OLD else REBATE_NEW_LIMIT


def derive_slab(
    *,
    other_income: Decimal,
    capital_gains_income: Decimal,
    tax_regime: str = TAX_REGIME_NEW,
    slab_override_pct: Decimal | None = None,
    surcharge_override_pct: Decimal | None = None,
    cess_pct: Decimal = CESS_RATE,
) -> SlabResolution:
    """Resolve the rate, surcharge, cess and rebate for a capital gain.

    ``other_income`` is the taxpayer's *taxable* other income — after
    their own deductions and standard deduction. The engine does not
    apply a standard deduction itself.
    """
    slabs = slabs_for_regime(tax_regime)
    total_income = other_income + capital_gains_income
    caveats: list[str] = []

    if slab_override_pct is not None:
        rate = slab_override_pct
        source = "override"
        caveats.append(
            f"Slab rate of {slab_override_pct}% was set manually, not derived from other income."
        )
    else:
        rate = marginal_rate(total_income, slabs)
        source = "derived"
        caveats.append(
            "Slab rate derived from taxable other income of "
            f"₹{other_income:,.0f} plus the capital gain. It is a marginal "
            "rate, not the taxpayer's total liability."
        )

    if surcharge_override_pct is not None:
        surcharge = surcharge_override_pct
        surcharge_source = "override"
    else:
        surcharge = surcharge_rate(total_income, tax_regime=tax_regime)
        surcharge_source = "derived"
        if surcharge > 0:
            caveats.append(
                f"Surcharge of {surcharge * 100:.0f}% applied on total income "
                f"of ₹{total_income:,.0f}."
            )

    return SlabResolution(
        rate_pct=rate,
        source=source,
        tax_regime=tax_regime,
        surcharge_pct=surcharge,
        surcharge_source=surcharge_source,
        cess_pct=cess_pct,
        rebate_amount=Decimal("0"),
        total_income_considered=total_income,
        marginal_relief_applied=surcharge_source == "derived",
        caveats=caveats,
    )


def compute_rebate(
    total_tax: Decimal,
    total_income: Decimal,
    tax_regime: str = TAX_REGIME_NEW,
) -> Decimal:
    """Section 87A rebate against the total tax on the capital gains.

    The rebate is capped at the tax payable, so it zeroes a low liability
    rather than creating a negative one.
    """
    if total_income > REBATE_INCOME_CEILING:
        return Decimal("0")
    return min(total_tax, rebate_limit(tax_regime))


def capital_gains_tax(
    gain: Decimal,
    *,
    rate: Decimal,
    regime_cess: Decimal,
    slab: SlabResolution,
) -> Decimal:
    """Tax on one capital-gains bucket.

    The rate is chosen by the caller: the regime's flat LTCG rate, the
    regime's flat STCG rate (Sec 111A), or the resolved marginal slab
    rate for foreign short-term gains. Surcharge is capped at 15% because
    the 25% and 37% rates do not apply to Sec 111A/112/112A income.
    """
    if gain <= 0:
        return Decimal("0")
    capped_surcharge = min(slab.surcharge_pct, CAPITAL_GAINS_SURCHARGE_CAP)
    tax = gain * rate
    return (tax * (1 + capped_surcharge) * (1 + regime_cess)).quantize(Decimal("0.01"))
