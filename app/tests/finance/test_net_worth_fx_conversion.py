"""Net-worth headline and the investing/spending summary must agree on
currency conversion.

Regression: NetWorthService._convert_to_reporting used to be a local
two-tier resolver (direct -> inverse, else None) while
app/core/currency.py::conversion_rate is a three-tier resolver
(direct -> inverse -> cross via USD). The net-worth path also built its
FX lookup from only the (currency, reporting) / (reporting, currency)
pairs, so the cross-via-USD leg was never even fetched.

Net effect: for a balance in a currency with no direct pair to the
workspace reporting currency, the net-worth headline reported the balance
as UNCONVERTIBLE (spending_total became null -> valuation_status
'partial' -> the UI showed an em dash) while the investing summary
converted the very same balance. Two screens, two answers.

These tests pin the two paths together.
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.core.currency import build_required_pairs, conversion_rate
from app.finance.models import FxRate
from app.finance.service import NetWorthService


def _fx(base: str, quote: str, rate: str) -> FxRate:
    return FxRate(
        base_currency_code=base,
        quote_currency_code=quote,
        rate=Decimal(rate),
    )


@pytest.fixture
def net_worth_service() -> NetWorthService:
    """A NetWorthService whose repositories are never touched.

    ``_convert_to_reporting`` is a pure function of (amount, currency,
    reporting_currency, fx_lookup) and reads no repository, so dummy
    collaborators are safe here.
    """
    return NetWorthService(
        session=MagicMock(),
        account_service=MagicMock(),
        summary_service=MagicMock(),
        cash_balance_repo=MagicMock(),
        setting_repo=MagicMock(),
        fx_rate_repo=MagicMock(),
        net_worth_snapshot_repo=MagicMock(),
    )


def test_direct_pair_is_used(net_worth_service: NetWorthService) -> None:
    fx = {("EUR", "INR"): _fx("EUR", "INR", "90.0")}
    assert net_worth_service._convert_to_reporting(Decimal("100"), "EUR", "INR", fx) == Decimal(
        "9000.0"
    )


def test_inverse_pair_is_used(net_worth_service: NetWorthService) -> None:
    fx = {("INR", "EUR"): _fx("INR", "EUR", "0.01")}
    assert net_worth_service._convert_to_reporting(Decimal("100"), "EUR", "INR", fx) == Decimal(
        "10000.0"
    )


def test_cross_via_usd_is_used(net_worth_service: NetWorthService) -> None:
    """The regression case: no direct or inverse pair, but a USD cross exists.

    The FX lookup the net-worth path builds now comes from
    build_required_pairs(), which includes the (X, USD) / (USD, X) legs.
    """
    fx = {
        ("EUR", "USD"): _fx("EUR", "USD", "1.10"),
        ("USD", "INR"): _fx("USD", "INR", "83.0"),
    }
    expected = conversion_rate("EUR", "INR", fx)
    assert expected is not None
    assert (
        net_worth_service._convert_to_reporting(Decimal("100"), "EUR", "INR", fx)
        == Decimal("100") * expected
    )


def test_net_worth_and_core_resolver_agree(net_worth_service: NetWorthService) -> None:
    """The two resolvers must not diverge for any pair combination."""
    fx = {
        ("EUR", "USD"): _fx("EUR", "USD", "1.10"),
        ("USD", "EUR"): _fx("USD", "EUR", "0.9090909091"),
        ("USD", "INR"): _fx("USD", "INR", "83.0"),
        ("INR", "USD"): _fx("INR", "USD", "0.0120481927"),
        ("EUR", "INR"): _fx("EUR", "INR", "91.30"),
        ("INR", "EUR"): _fx("INR", "EUR", "0.0109529014"),
    }
    for currency in ("EUR", "GBP", "USD"):
        for reporting in ("INR", "USD"):
            core = conversion_rate(currency, reporting, fx)
            net_worth = net_worth_service._convert_to_reporting(
                Decimal("100"), currency, reporting, fx
            )
            if core is None:
                assert net_worth is None, (
                    f"{currency}->{reporting}: net worth converted but the core resolver did not"
                )
            else:
                assert net_worth == Decimal("100") * core, f"{currency}->{reporting} diverged"


def test_build_required_pairs_includes_usd_legs() -> None:
    """The pair set the net-worth path requests must include the USD legs."""
    pairs = build_required_pairs(["EUR"], "INR")
    assert ("EUR", "USD") in pairs
    assert ("USD", "EUR") in pairs
    assert ("EUR", "INR") in pairs
    assert ("INR", "EUR") in pairs
