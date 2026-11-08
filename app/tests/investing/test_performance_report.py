"""``get_performance`` payload shaping and period windows (spec-103)."""

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.investing.performance_report import (
    VALID_PERIODS,
    build_performance_report,
    period_start,
)
from app.investing.schemas import (
    AccountReturnMetrics,
    OverallReturnMetrics,
    PositionMetrics,
    ReturnMetricsResponse,
)

ACCOUNT_US = uuid.uuid4()
ACCOUNT_IN = uuid.uuid4()


def _position(invested="0", market_value="0") -> PositionMetrics:
    return PositionMetrics(invested=Decimal(invested), market_value=Decimal(market_value))


def _account(
    account_id, name, currency, xirr, invested, market_value, realized="0", unrealized="0"
):
    return AccountReturnMetrics(
        account_id=account_id,
        account_name=name,
        currency=currency,
        xirr=None if xirr is None else Decimal(xirr),
        realized=Decimal(realized),
        unrealized=Decimal(unrealized),
        open=_position(invested, market_value),
        closed=_position(),
    )


def _metrics(status="current") -> ReturnMetricsResponse:
    return ReturnMetricsResponse(
        currency="INR",
        valuation_status=status,
        overall=OverallReturnMetrics(
            xirr=Decimal("0.1500"),
            realized=Decimal("10"),
            unrealized=Decimal("90"),
            open=_position("1000", "1100"),
            closed=_position(),
        ),
        by_account=[
            _account(ACCOUNT_US, "US", "USD", "0.2000", "100", "120", "5", "20"),
            _account(ACCOUNT_IN, "IN", "INR", None, "1000", "1100", "5", "70"),
        ],
        by_currency=[],
    )


def _holding(account_id, name, symbol, currency, xirr):
    return {
        "account_id": account_id,
        "account_name": name,
        "symbol": symbol,
        "currency": currency,
        "is_open": True,
        "xirr": None if xirr is None else Decimal(xirr),
        "annualized_return_pct": None,
        "annualization_reliable": False,
        "holding_days": 10,
        "total_return_pct": Decimal("5"),
        "realized": Decimal("0"),
        "unrealized": Decimal("20"),
        "market_value": Decimal("120"),
        "invested": Decimal("100"),
    }


TWR = {
    "status": "ok",
    "currency": "USD",
    "twr_pct": "4.00",
    "start_value": "100.00",
    "end_value": "120.00",
    "net_external_flows": "10.00",
    "warnings": [],
}


def _report(rates, account_public_id=None, metrics=None, twr=None):
    return build_performance_report(
        metrics=metrics or _metrics(),
        positions=[
            _holding(ACCOUNT_US, "US", "AAPL", "USD", "0.2000"),
            _holding(ACCOUNT_IN, "IN", "INFY", "INR", None),
        ],
        twr=twr or TWR,
        rate_for=lambda ccy: Decimal("1") if ccy == "INR" else rates.get(ccy),
        reporting_currency="INR",
        account_public_id=account_public_id,
    )


def test_converts_at_latest_fx_and_serialises_money_as_strings():
    report = _report({"USD": Decimal("83.5")})
    assert report["valuation_status"] == "current"
    us = next(a for a in report["by_account"] if a["currency"] == "USD")
    assert us["invested"] == "100.00"
    assert us["converted"] == {
        "currency": "INR",
        "invested": "8350.00",
        "market_value": "10020.00",
        "realized": "417.50",
        "unrealized": "1670.00",
    }
    assert us["xirr"] == "0.2000" and us["xirr_pct"] == "20.00" and us["xirr_status"] == "ok"
    # Overall amounts are the sum of per-account INR amounts at that rate.
    assert report["overall"]["converted"]["invested"] == "9350.00"
    assert report["overall"]["converted"]["market_value"] == "11120.00"
    assert report["overall"]["scope"] == "workspace"
    assert report["time_weighted"]["converted"]["end_value"] == "10020.00"
    assert report["time_weighted"]["converted"]["net_external_flows"] == "835.00"
    assert not any("No FX rate" in w for w in report["warnings"])


def test_missing_fx_is_explicit_null_and_warning_never_zero():
    report = _report({})
    assert report["valuation_status"] == "conversion_required"
    us = next(a for a in report["by_account"] if a["currency"] == "USD")
    assert us["converted"]["invested"] is None
    assert us["converted"]["market_value"] is None
    assert us["invested"] == "100.00"  # native stays intact
    inr = next(a for a in report["by_account"] if a["currency"] == "INR")
    assert inr["converted"]["invested"] == "1000.00"
    # The overall INR total would be incomplete -> null rather than a partial sum.
    assert report["overall"]["converted"]["invested"] is None
    assert report["overall"]["converted"]["market_value"] is None
    assert report["time_weighted"]["converted"]["end_value"] is None
    assert any("No FX rate to INR for ['USD']" in w for w in report["warnings"])
    holding = next(h for h in report["by_holding"] if h["symbol"] == "AAPL")
    assert holding["converted"]["market_value"] is None


def test_xirr_not_computable_is_a_status_not_an_error():
    report = _report({"USD": Decimal("83")})
    inr = next(a for a in report["by_account"] if a["currency"] == "INR")
    assert inr["xirr"] is None and inr["xirr_pct"] is None
    assert inr["xirr_status"] == "not_computable"
    infy = next(h for h in report["by_holding"] if h["symbol"] == "INFY")
    assert infy["xirr_status"] == "not_computable"
    assert any("XIRR needs" in w for w in report["warnings"])
    assert report["xirr_basis"] == "since_inception"


def test_overall_conversion_required_status_from_service():
    report = _report({"USD": Decimal("83")}, metrics=_metrics("conversion_required"))
    assert report["overall"]["xirr_status"] == "conversion_required"
    assert any("Overall XIRR" in w for w in report["warnings"])


def test_account_filter_scopes_accounts_holdings_and_overall():
    report = _report({"USD": Decimal("83")}, account_public_id=ACCOUNT_US)
    assert [a["account_id"] for a in report["by_account"]] == [str(ACCOUNT_US)]
    assert {h["symbol"] for h in report["by_holding"]} == {"AAPL"}
    assert report["overall"]["scope"] == "account"
    assert report["overall"]["xirr"] == "0.2000"
    assert report["overall"]["converted"]["invested"] == "8300.00"


def test_twr_warnings_are_surfaced():
    twr = {**TWR, "twr_pct": None, "status": "missing_fx_for_flows", "warnings": ["boom"]}
    report = _report({"USD": Decimal("83")}, twr=twr)
    assert "boom" in report["warnings"]
    assert report["time_weighted"]["status"] == "missing_fx_for_flows"


@pytest.mark.parametrize(
    "period,today,expected",
    [
        ("all", date(2026, 11, 8), None),
        ("ytd", date(2026, 11, 8), date(2026, 1, 1)),
        ("1m", date(2026, 11, 8), date(2026, 10, 8)),
        ("1m", date(2026, 3, 31), date(2026, 2, 28)),
        ("1m", date(2026, 1, 15), date(2025, 12, 15)),
        ("3m", date(2026, 1, 31), date(2025, 10, 31)),
        ("6m", date(2026, 8, 31), date(2026, 2, 28)),
        ("1y", date(2028, 2, 29), date(2027, 2, 28)),
        ("3y", date(2026, 11, 8), date(2023, 11, 8)),
        ("5y", date(2026, 11, 8), date(2021, 11, 8)),
    ],
)
def test_period_start(period, today, expected):
    assert period_start(period, today) == expected


def test_every_valid_period_has_a_window():
    for period in VALID_PERIODS:
        period_start(period, date(2026, 11, 8))
