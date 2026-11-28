"""Cross-currency conversion in ReturnMetricsService must not query FX per cash flow.

Production measured ~300 DB queries for one GET /investing/performance/returns: every flow of
every non-reporting-currency position awaited its own ``resolve_historical_rate`` call, although
many flows share the same (currency, date).
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.investing.return_metrics_service import ReturnMetricsService, _Flow, _Position


class _CountingFxService:
    """Real behaviour, observable cost: a rate table plus a call log."""

    def __init__(self, rates: dict[tuple[str, date], Decimal], today_rate: Decimal):
        self._rates = rates
        self._today_rate = today_rate
        self.calls: list[tuple[str, str, datetime]] = []

    async def resolve_historical_rate(self, workspace_id, base_currency, quote_currency, as_of):
        self.calls.append((base_currency.upper(), quote_currency.upper(), as_of))
        if as_of.date() >= datetime.now(UTC).date():
            return self._today_rate, "system"
        rate = self._rates.get((base_currency.upper(), as_of.date()))
        return (rate, "system") if rate is not None else None


def _service(fx) -> ReturnMetricsService:
    return ReturnMetricsService(
        order_repo=MagicMock(),
        holding_repo=MagicMock(),
        holding_price_repo=MagicMock(),
        dividend_repo=MagicMock(),
        account_repo=MagicMock(),
        net_worth_snapshot_repo=MagicMock(),
        fx_rate_service=fx,
    )


def _usd_position(symbol: str, flow_dates: list[date]) -> _Position:
    return _Position(
        account_id=1,
        symbol=symbol,
        currency="USD",
        is_open=True,
        flows=[_Flow(when=d, amount=Decimal("-100"), is_contribution=True) for d in flow_dates],
        realized=Decimal("10"),
        market_value=Decimal("200"),
        book_value=Decimal("100"),
    )


@pytest.mark.anyio
async def test_conversion_resolves_each_currency_date_pair_once():
    d1, d2 = date(2025, 1, 10), date(2025, 2, 10)
    fx = _CountingFxService({("USD", d1): Decimal("80"), ("USD", d2): Decimal("82")}, Decimal("90"))
    # Three positions, six flows, but only two distinct dates.
    positions = [
        _usd_position("AAA", [d1, d2]),
        _usd_position("BBB", [d1, d2]),
        _usd_position("CCC", [d1, d2]),
    ]

    converted, ok = await _service(fx)._convert_positions(1, positions, "INR")

    assert ok is True
    # 2 distinct dates + 1 "today" lookup for the one currency, not 3 * (2 + 1) = 9.
    assert len(fx.calls) == 3
    # Conversion is unchanged: each flow still uses its own date's rate.
    assert [f.amount for f in converted[0].flows] == [Decimal("-8000"), Decimal("-8200")]
    assert [f.amount for f in converted[2].flows] == [Decimal("-8000"), Decimal("-8200")]
    assert converted[1].market_value == Decimal("200") * Decimal("90")
    assert converted[1].currency == "INR"


@pytest.mark.anyio
async def test_conversion_still_reports_failure_when_a_rate_is_missing():
    d1, missing = date(2025, 1, 10), date(2025, 3, 3)
    fx = _CountingFxService({("USD", d1): Decimal("80")}, Decimal("90"))
    positions = [_usd_position("AAA", [d1, missing]), _usd_position("BBB", [d1, missing])]

    converted, ok = await _service(fx)._convert_positions(1, positions, "INR")

    assert (converted, ok) == ([], False)
    # The missing pair is not retried for the second position before giving up.
    assert len(fx.calls) == 2
