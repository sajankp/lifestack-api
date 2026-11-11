"""Holding valuation: stale-price annotation and cost-basis-fallback scope."""

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.investing.performance_service import InvestingSummaryService
from app.mcp.tools.portfolio import _add_holding_reporting_valuation


def _holding(quantity: str = "10", currency: str = "USD") -> SimpleNamespace:
    return SimpleNamespace(quantity=Decimal(quantity), avg_cost=Decimal("100"), currency=currency)


def _price_row(price_date: date, unit_price: str = "200") -> SimpleNamespace:
    return SimpleNamespace(price_date=price_date, unit_price=Decimal(unit_price), source="api")


def _data() -> dict:
    return {}


def test_valuation_stays_current_within_stale_window() -> None:
    data = _data()
    _add_holding_reporting_valuation(
        data,
        _holding(),
        _price_row(date(2026, 11, 9)),
        "USD",
        {},
        None,
        stale_days=7,
    )
    assert data["valuation_status"] == "current"


def test_valuation_goes_stale_outside_stale_window() -> None:
    # Today is 2026-11-10; a 2026-10-31 price is 10 days old > 7-day window.
    data = _data()
    _add_holding_reporting_valuation(
        data,
        _holding(),
        _price_row(date(2026, 10, 31)),
        "USD",
        {},
        None,
        stale_days=7,
    )
    assert data["valuation_status"] == "stale"
    assert data["price_as_of"] == "2026-10-31"


def test_valuation_boundary_is_seven_days_inclusive() -> None:
    # Exactly 7 days old is still current; the window is "older than".
    # Use a date relative to today so the boundary holds regardless of when CI runs.
    data = _data()
    _add_holding_reporting_valuation(
        data,
        _holding(),
        _price_row(date.today() - timedelta(days=7)),
        "USD",
        {},
        None,
        stale_days=7,
    )
    assert data["valuation_status"] == "current"


def test_zero_quantity_never_goes_stale() -> None:
    data = _data()
    holding = SimpleNamespace(quantity=Decimal("0"), avg_cost=Decimal("100"), currency="USD")
    _add_holding_reporting_valuation(
        data, holding, _price_row(date(2026, 10, 31)), "USD", {}, None, stale_days=7
    )
    assert data["valuation_status"] == "zero_quantity"


def _summary_service() -> tuple[
    InvestingSummaryService, MagicMock, MagicMock, MagicMock, MagicMock, MagicMock, MagicMock
]:
    holding_repo = MagicMock()
    cash_repo = MagicMock()
    finance_setting_repo = MagicMock()
    fx_rate_repo = MagicMock()
    holding_price_repo = MagicMock()
    snapshot_repo = MagicMock()
    account_repo = MagicMock()
    return (
        InvestingSummaryService(
            holding_repo=holding_repo,
            cash_repo=cash_repo,
            finance_setting_repo=finance_setting_repo,
            fx_rate_repo=fx_rate_repo,
            holding_price_repo=holding_price_repo,
            snapshot_repo=snapshot_repo,
            account_repo=account_repo,
        ),
        holding_repo,
        cash_repo,
        finance_setting_repo,
        holding_price_repo,
        snapshot_repo,
        account_repo,
    )


def _mock_get_all(session_mock: MagicMock, holdings: list) -> None:
    session_mock.holding_repo.get_all = MagicMock(return_value=(holdings, len(holdings)))
    session_mock.cash_repo.get_latest_per_account_currency = MagicMock(return_value=[])
    session_mock.account_repo.list_workspace_accounts = MagicMock(return_value=([], 0))
    session_mock.holding_price_repo.latest_prices_on_or_before_bulk = MagicMock(return_value={})
    session_mock.snapshot_repo.latest_before = MagicMock(return_value=None)


@pytest.mark.asyncio
async def test_zero_quantity_holding_without_price_does_not_trigger_fallback() -> None:
    # Closed positions carry no value with or without a stored price: they
    # must not force get_investing_summary into cost_basis_fallback.
    (
        service,
        holding_repo,
        cash_repo,
        finance_setting_repo,
        holding_price_repo,
        snapshot_repo,
        account_repo,
    ) = _summary_service()
    settings = SimpleNamespace(reporting_currency_code="INR", stale_price_days=7)
    finance_setting_repo.get_by_workspace = AsyncMock(return_value=settings)
    holdings = [
        SimpleNamespace(id=1, quantity=Decimal("0"), avg_cost=Decimal("50"), currency="INR")
    ]
    holding_repo.get_all = AsyncMock(return_value=(holdings, 1))
    cash_repo.get_latest_per_account_currency = AsyncMock(return_value=[])
    account_repo.list_workspace_accounts = AsyncMock(return_value=([], 0))
    holding_price_repo.latest_prices_on_or_before_bulk = AsyncMock(return_value={})
    snapshot_repo.latest_before = AsyncMock(return_value=None)

    summary = await service.get_summary(1)
    # Reporting currency (INR) is configured, so the non-fallback status is
    # "converted_available"; the zero-quantity holding must not trigger
    # cost_basis_fallback and contributes nothing to value.
    assert summary.valuation_status == "converted_available"
    assert summary.portfolio_value == Decimal("0")


@pytest.mark.asyncio
async def test_open_holding_without_price_triggers_fallback() -> None:
    (
        service,
        holding_repo,
        cash_repo,
        finance_setting_repo,
        holding_price_repo,
        snapshot_repo,
        account_repo,
    ) = _summary_service()
    settings = SimpleNamespace(reporting_currency_code="INR", stale_price_days=7)
    finance_setting_repo.get_by_workspace = AsyncMock(return_value=settings)
    holdings = [
        SimpleNamespace(id=1, quantity=Decimal("10"), avg_cost=Decimal("50"), currency="INR")
    ]
    holding_repo.get_all = AsyncMock(return_value=(holdings, 1))
    cash_repo.get_latest_per_account_currency = AsyncMock(return_value=[])
    account_repo.list_workspace_accounts = AsyncMock(return_value=([], 0))
    holding_price_repo.latest_prices_on_or_before_bulk = AsyncMock(return_value={})
    snapshot_repo.latest_before = AsyncMock(return_value=None)

    summary = await service.get_summary(1)
    assert summary.valuation_status == "cost_basis_fallback"
    assert summary.portfolio_value == Decimal("500")
