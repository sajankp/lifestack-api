"""XIRR: money-weighted annualized return over dated cash flows (spec-071).

Pure, side-effect-free. Solves r in:  sum(cashflow_i / (1+r)^(days_i/365)) = 0
using Newton's method with a bisection fallback, bounded iterations and a
bounded search range. Day-count is fixed at 365 (INV pinned in the spec so
test fixtures don't churn).

INV-4 — solver safety: never throws, never hangs. Returns None (the caller
falls back to annualized-%) when the flows are degenerate (fewer than two
flows, all same sign, sub-day span) or the solver fails to converge within
the iteration/range bounds.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

DAY_COUNT = Decimal("365")
_MAX_NEWTON_ITERATIONS = 100
_MAX_BISECTION_ITERATIONS = 200
_TOLERANCE = Decimal("1e-7")
# Search bounded to r in (-0.9999, 100) -- a rate <= -100% is nonsensical
# (total loss faster than instantaneously) and >10000% annualized is beyond
# any real scenario; bounding avoids runaway iteration on pathological input.
_MIN_RATE = Decimal("-0.9999")
_MAX_RATE = Decimal("100")


@dataclass(frozen=True)
class CashFlow:
    when: date
    amount: Decimal


def _npv(flows: list[CashFlow], rate: Decimal, t0: date) -> Decimal:
    total = Decimal("0")
    one_plus_r = Decimal("1") + rate
    for flow in flows:
        days = Decimal((flow.when - t0).days)
        exponent = days / DAY_COUNT
        # Decimal has no fractional pow; float round-trip is fine here since
        # this is a root-finding aid, not the final stored value (the final
        # accepted rate is returned as-is from the solved Decimal state).
        factor = Decimal(str(float(one_plus_r) ** float(exponent)))
        total += flow.amount / factor
    return total


def _npv_derivative(flows: list[CashFlow], rate: Decimal, t0: date) -> Decimal:
    total = Decimal("0")
    one_plus_r = Decimal("1") + rate
    for flow in flows:
        days = Decimal((flow.when - t0).days)
        exponent = days / DAY_COUNT
        if exponent == 0:
            continue
        factor = Decimal(str(float(one_plus_r) ** float(exponent + 1)))
        total += -exponent * flow.amount / factor
    return total


def _is_degenerate(flows: list[CashFlow]) -> bool:
    if len(flows) < 2:
        return True
    signs = {1 if f.amount > 0 else (-1 if f.amount < 0 else 0) for f in flows}
    if len(signs - {0}) < 2:
        return True
    span_days = (max(f.when for f in flows) - min(f.when for f in flows)).days
    return span_days < 1


def solve_xirr(flows: list[CashFlow]) -> Decimal | None:
    """Solve for XIRR. Returns None for degenerate input or non-convergence.

    flows must contain at least one negative and one positive amount and
    span at least one day; the solver never raises."""
    if _is_degenerate(flows):
        return None

    t0 = min(f.when for f in flows)

    try:
        rate = Decimal("0.1")
        for _ in range(_MAX_NEWTON_ITERATIONS):
            npv = _npv(flows, rate, t0)
            if abs(npv) < _TOLERANCE:
                if _MIN_RATE < rate < _MAX_RATE:
                    return rate
                break
            deriv = _npv_derivative(flows, rate, t0)
            if deriv == 0:
                break
            next_rate = rate - npv / deriv
            if next_rate <= _MIN_RATE or next_rate >= _MAX_RATE or next_rate != next_rate:
                break
            rate = next_rate
    except (OverflowError, ZeroDivisionError, ValueError):
        pass

    # Bisection fallback over the bounded range, requiring a sign change.
    try:
        lo, hi = _MIN_RATE, _MAX_RATE
        npv_lo = _npv(flows, lo, t0)
        npv_hi = _npv(flows, hi, t0)
        if npv_lo == 0:
            return lo
        if npv_hi == 0:
            return hi
        if (npv_lo > 0) == (npv_hi > 0):
            return None
        for _ in range(_MAX_BISECTION_ITERATIONS):
            mid = (lo + hi) / 2
            npv_mid = _npv(flows, mid, t0)
            if abs(npv_mid) < _TOLERANCE:
                return mid
            if (npv_mid > 0) == (npv_lo > 0):
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2
    except (OverflowError, ZeroDivisionError, ValueError):
        return None
