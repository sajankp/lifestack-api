from datetime import date, timedelta
from decimal import Decimal

from app.investing.xirr import CashFlow, solve_xirr


def _d(days_from: date, offset: int) -> date:
    return days_from + timedelta(days=offset)


def test_xirr_single_buy_single_sell_known_rate():
    """1000 invested, 1100 back exactly 365 days later -> exactly 10%."""
    start = date(2020, 1, 1)
    flows = [
        CashFlow(when=start, amount=Decimal("-1000")),
        CashFlow(when=_d(start, 365), amount=Decimal("1100")),
    ]
    rate = solve_xirr(flows)
    assert rate is not None
    assert abs(rate - Decimal("0.10")) < Decimal("0.0005")


def test_xirr_matches_hand_computed_two_year_double():
    """1000 invested, 4000 back exactly 730 days later -> ~100% annualized
    (money doubled twice in two years: (1+r)^2 = 4 -> r = 1.0)."""
    start = date(2020, 1, 1)
    flows = [
        CashFlow(when=start, amount=Decimal("-1000")),
        CashFlow(when=_d(start, 730), amount=Decimal("4000")),
    ]
    rate = solve_xirr(flows)
    assert rate is not None
    assert abs(rate - Decimal("1.0")) < Decimal("0.01")


def test_xirr_multiple_contributions_and_terminal_value():
    """Two buys plus a terminal value; just proves convergence + a
    plausible positive-return magnitude (not a closed-form check)."""
    start = date(2021, 1, 1)
    flows = [
        CashFlow(when=start, amount=Decimal("-500")),
        CashFlow(when=_d(start, 180), amount=Decimal("-500")),
        CashFlow(when=_d(start, 365), amount=Decimal("1200")),
    ]
    rate = solve_xirr(flows)
    assert rate is not None
    assert Decimal("0") < rate < Decimal("2")


def test_xirr_returns_none_for_single_flow():
    flows = [CashFlow(when=date(2020, 1, 1), amount=Decimal("-1000"))]
    assert solve_xirr(flows) is None


def test_xirr_returns_none_for_same_sign_flows():
    start = date(2020, 1, 1)
    flows = [
        CashFlow(when=start, amount=Decimal("-500")),
        CashFlow(when=_d(start, 30), amount=Decimal("-500")),
    ]
    assert solve_xirr(flows) is None


def test_xirr_returns_none_for_sub_day_span():
    start = date(2020, 1, 1)
    flows = [
        CashFlow(when=start, amount=Decimal("-1000")),
        CashFlow(when=start, amount=Decimal("1000")),
    ]
    assert solve_xirr(flows) is None


def test_xirr_returns_none_for_empty_flows():
    assert solve_xirr([]) is None


def test_xirr_handles_large_loss_without_raising():
    """A near-total loss (rate approaching -100%) must not raise or hang --
    it may return None (non-convergent) or a bounded rate, but never throws."""
    start = date(2020, 1, 1)
    flows = [
        CashFlow(when=start, amount=Decimal("-100000")),
        CashFlow(when=_d(start, 365), amount=Decimal("1")),
    ]
    result = solve_xirr(flows)
    assert result is None or (Decimal("-1") < result < Decimal("0"))


def test_xirr_handles_extreme_gain_without_raising():
    start = date(2020, 1, 1)
    flows = [
        CashFlow(when=start, amount=Decimal("-1")),
        CashFlow(when=_d(start, 30), amount=Decimal("1000000")),
    ]
    result = solve_xirr(flows)
    assert result is None or result > Decimal("0")
