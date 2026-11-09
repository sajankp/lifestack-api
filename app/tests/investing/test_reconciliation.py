"""Unit tests for holdings reconciliation (spec-103): parsing, normalisation, diff."""

import uuid
from decimal import Decimal

import pytest

from app.investing.reconciliation import (
    MAX_EXTERNAL_ENTRIES,
    LifestackHolding,
    ReconciliationInputError,
    diff_holdings,
    normalize_isin,
    normalize_symbol,
    parse_external_list,
    parse_tolerances,
)

ACCOUNT_A = uuid.uuid4()
ACCOUNT_B = uuid.uuid4()


def _ls(
    symbol: str,
    quantity: str,
    avg_cost: str,
    *,
    isin: str | None = None,
    account: uuid.UUID = ACCOUNT_A,
    currency: str = "INR",
) -> LifestackHolding:
    return LifestackHolding(
        public_id=uuid.uuid4(),
        account_internal_id=1 if account == ACCOUNT_A else 2,
        account_public_id=account,
        account_name="Zerodha" if account == ACCOUNT_A else "INDmoney",
        symbol=normalize_symbol(symbol),
        isin=isin,
        quantity=Decimal(quantity),
        avg_cost=Decimal(avg_cost),
        currency=currency,
    )


def _diff(external: list[dict], lifestack: list[LifestackHolding], **kwargs):
    return diff_holdings(parse_external_list(external), lifestack, **kwargs)


def _status(result, symbol):
    return [i["status"] for i in result["items"] if i["symbol"] == symbol]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("reliance", "RELIANCE"),
        ("NSE:RELIANCE", "RELIANCE"),
        ("RELIANCE.NS", "RELIANCE"),
        ("reliance-eq", "RELIANCE"),
        ("  TATA MOTORS ", "TATAMOTORS"),
        ("BRK.B", "BRK.B"),
        ("NASDAQ:AAPL", "AAPL"),
    ],
)
def test_normalize_symbol(raw, expected):
    assert normalize_symbol(raw) == expected


def test_normalize_isin():
    assert normalize_isin(" ine002a01018 ") == "INE002A01018"
    assert normalize_isin("not-an-isin") is None
    assert normalize_isin(None) is None


def test_all_five_buckets_with_actionable_suggestions():
    lifestack = [
        _ls("INFY", "10", "1500"),  # matched
        _ls("TCS", "10", "3000"),  # quantity drift (external 12)
        _ls("HDFCBANK", "5", "1500"),  # cost basis drift (external 1530)
        _ls("ITC", "100", "400"),  # absent from external -> missing_at_depository
    ]
    result = _diff(
        [
            {"symbol": "INFY", "quantity": "10", "avg_cost": "1500"},
            {"symbol": "TCS", "quantity": "12", "avg_cost": "3000"},
            {"symbol": "HDFCBANK", "quantity": "5", "avg_cost": "1530"},
            {"symbol": "WIPRO", "quantity": "7", "avg_cost": "450", "currency": "INR"},
        ],
        lifestack,
    )
    assert _status(result, "INFY") == ["matched"]
    assert _status(result, "TCS") == ["quantity_drift"]
    assert _status(result, "HDFCBANK") == ["cost_basis_drift"]
    assert _status(result, "WIPRO") == ["missing_in_lifestack"]
    assert _status(result, "ITC") == ["missing_at_depository"]
    assert result["summary"] == {
        "total_external": 4,
        "total_lifestack": 4,
        "missing_in_lifestack": 1,
        "quantity_drift": 1,
        "cost_basis_drift": 1,
        "missing_at_depository": 1,
        "matched": 1,
    }
    by_symbol = {s["symbol"]: s for s in result["suggestions"]}
    assert "INFY" not in by_symbol  # matched rows need no suggestion
    assert by_symbol["WIPRO"]["action"] == "record_buy_orders"
    assert by_symbol["WIPRO"]["quantity"] == "7"
    assert by_symbol["WIPRO"]["price_per_unit"] == "450"
    assert by_symbol["TCS"]["action"] == "record_buy_orders"
    assert by_symbol["TCS"]["quantity"] == "2"
    assert by_symbol["HDFCBANK"]["action"] == "review_order_fees"
    assert by_symbol["HDFCBANK"]["cost_delta_pct"] == "2.00"
    assert by_symbol["HDFCBANK"]["implied_total_cost_difference"] == "150.00"
    assert "brokerage_fee" in by_symbol["HDFCBANK"]["message"]
    assert by_symbol["ITC"]["action"] == "verify_or_remove_stale_holding"


def test_excess_quantity_suggests_sell():
    result = _diff([{"symbol": "TCS", "quantity": "8"}], [_ls("TCS", "10", "3000")])
    suggestion = result["suggestions"][0]
    assert suggestion["status"] == "quantity_drift"
    assert suggestion["action"] == "record_sell_orders"
    assert suggestion["quantity"] == "2"
    assert result["items"][0]["quantity_delta"] == "-2"


def test_float_noise_is_within_tolerance():
    # 0.1 + 0.2 style artefact and a fractional-share rounding wobble.
    result = _diff(
        [{"symbol": "AAPL", "quantity": 0.1 + 0.2, "avg_cost": 150.00000000001}],
        [_ls("AAPL", "0.3", "150", currency="USD")],
    )
    assert result["items"][0]["status"] == "matched"


def test_quantity_outside_absolute_tolerance_is_drift():
    result = _diff([{"symbol": "AAPL", "quantity": "10.01"}], [_ls("AAPL", "10", "150")])
    assert result["items"][0]["status"] == "quantity_drift"
    # ...unless the caller widens the tolerance.
    widened = _diff(
        [{"symbol": "AAPL", "quantity": "10.01"}],
        [_ls("AAPL", "10", "150")],
        quantity_tolerance=Decimal("0.05"),
    )
    assert widened["items"][0]["status"] == "matched"


def test_cost_tolerance_boundary():
    lifestack = [_ls("INFY", "10", "1000")]
    inside = _diff([{"symbol": "INFY", "quantity": "10", "avg_cost": "1004.9"}], lifestack)
    outside = _diff([{"symbol": "INFY", "quantity": "10", "avg_cost": "1005.1"}], lifestack)
    assert inside["items"][0]["status"] == "matched"
    assert outside["items"][0]["status"] == "cost_basis_drift"
    # Cost is optional: no external avg_cost never produces cost drift.
    no_cost = _diff([{"symbol": "INFY", "quantity": "10"}], lifestack)
    assert no_cost["items"][0]["status"] == "matched"


def test_symbol_variants_match_the_same_holding():
    lifestack = [_ls("RELIANCE", "10", "2500")]
    for variant in ("nse:reliance", "RELIANCE.NS", "RELIANCE-EQ", " Reliance "):
        result = _diff([{"symbol": variant, "quantity": "10", "avg_cost": "2500"}], lifestack)
        assert result["items"][0]["status"] == "matched", variant


def test_isin_wins_over_a_differing_symbol():
    lifestack = [_ls("OLDNAME", "10", "100", isin="INE002A01018")]
    result = _diff(
        [{"symbol": "RELIANCE", "isin": "ine002a01018", "quantity": "10", "avg_cost": "100"}],
        lifestack,
    )
    assert result["items"][0]["status"] == "matched"
    assert result["items"][0]["isin"] == "INE002A01018"
    assert result["summary"]["missing_at_depository"] == 0


def test_isin_only_external_row():
    result = _diff(
        [{"isin": "INE002A01018", "quantity": "3"}],
        [_ls("RELIANCE", "3", "2500", isin="INE002A01018")],
    )
    assert result["items"][0]["status"] == "matched"


def test_external_account_scoping_and_aggregation_across_accounts():
    lifestack = [
        _ls("INFY", "6", "1000", account=ACCOUNT_A),
        _ls("INFY", "4", "1500", account=ACCOUNT_B),
    ]
    # No account on the external row: compare against the aggregate (10 @ 1200).
    agg = _diff([{"symbol": "INFY", "quantity": "10", "avg_cost": "1200"}], lifestack)
    assert agg["items"][0]["status"] == "matched"
    assert agg["items"][0]["lifestack"]["quantity"] == "10"
    assert agg["items"][0]["lifestack"]["avg_cost"] == "1200"
    assert len(agg["items"][0]["lifestack"]["holdings"]) == 2
    # With an account on the row only that account's holding is compared; the
    # other account's holding is reported as missing at the depository.
    scoped = _diff(
        [{"symbol": "INFY", "quantity": "6", "avg_cost": "1000", "account_id": str(ACCOUNT_A)}],
        lifestack,
    )
    assert scoped["summary"]["matched"] == 1
    assert scoped["summary"]["missing_at_depository"] == 1


def test_duplicate_external_rows_are_merged_with_weighted_cost():
    result = _diff(
        [
            {"symbol": "INFY", "quantity": "4", "avg_cost": "1000", "external_ref": "a"},
            {"symbol": "INFY.NS", "quantity": "6", "avg_cost": "1500", "external_ref": "b"},
        ],
        [_ls("INFY", "10", "1300")],
    )
    item = result["items"][0]
    assert item["status"] == "matched"
    assert item["external"]["quantity"] == "10"
    assert item["external"]["avg_cost"] == "1300"
    assert item["external"]["row_indexes"] == [0, 1]
    assert item["external"]["external_refs"] == ["a", "b"]


def test_currency_mismatch_skips_cost_comparison():
    result = _diff(
        [{"symbol": "AAPL", "quantity": "1", "avg_cost": "12000", "currency": "INR"}],
        [_ls("AAPL", "1", "150", currency="USD")],
    )
    assert result["items"][0]["status"] == "matched"
    assert "currency" in result["items"][0]["suggestion"]["message"].lower()


def test_zero_external_quantity_with_lifestack_position_is_drift():
    result = _diff([{"symbol": "TCS", "quantity": "0"}], [_ls("TCS", "5", "3000")])
    assert result["items"][0]["status"] == "quantity_drift"
    assert result["items"][0]["suggestion"]["action"] == "record_sell_orders"


def test_zero_external_quantity_unknown_symbol_is_not_flagged():
    result = _diff([{"symbol": "GHOST", "quantity": "0"}], [])
    assert result["items"][0]["status"] == "matched"
    assert result["suggestions"] == []


def test_ordering_is_deterministic():
    lifestack = [_ls("ZZZ", "1", "1"), _ls("AAA", "1", "1"), _ls("MMM", "1", "1")]
    first = _diff([{"symbol": "NEW", "quantity": "1"}], lifestack)
    second = _diff([{"symbol": "NEW", "quantity": "1"}], list(reversed(lifestack)))
    assert [i["symbol"] for i in first["items"]] == ["NEW", "AAA", "MMM", "ZZZ"]
    assert [i["symbol"] for i in first["items"]] == [i["symbol"] for i in second["items"]]


def test_money_is_serialised_as_strings():
    result = _diff(
        [{"symbol": "INFY", "quantity": 10, "avg_cost": 1500.5}], [_ls("INFY", "10", "1500.5")]
    )
    item = result["items"][0]
    assert item["external"]["quantity"] == "10"
    assert item["external"]["avg_cost"] == "1500.5"
    assert item["lifestack"]["avg_cost"] == "1500.5"
    assert all(isinstance(v, str) for v in (item["quantity_delta"],))


@pytest.mark.parametrize(
    "payload,message",
    [
        ([], "between 1 and"),
        ([{"quantity": "1"}], "symbol or isin"),
        ([{"symbol": "X"}], "quantity is required"),
        ([{"symbol": "X", "quantity": "abc"}], "decimal"),
        ([{"symbol": "X", "quantity": "NaN"}], "finite"),
        ([{"symbol": "X", "quantity": "Infinity"}], "finite"),
        ([{"symbol": "X", "quantity": "-1"}], "negative"),
        ([{"symbol": "X", "quantity": True}], "decimal"),
        ([{"symbol": "X", "quantity": "1", "avg_cost": "-3"}], "negative"),
        ([{"symbol": "X", "quantity": "1", "avg_cost": "zzz"}], "decimal"),
        ([{"symbol": "X", "quantity": "1", "currency": "RUPEES"}], "3-letter"),
        ([{"symbol": "X", "quantity": "1", "account_id": "nope"}], "UUID"),
        ([{"symbol": "X", "quantity": "1", "isin": "bad"}], "ISIN"),
        ([{"symbol": "X" * 65, "quantity": "1"}], "too long"),
        (["INFY"], "object"),
    ],
)
def test_invalid_input_is_rejected_with_a_row_message(payload, message):
    with pytest.raises(ReconciliationInputError) as exc:
        parse_external_list(payload)
    assert message in str(exc.value)


def test_input_size_cap():
    ok = [{"symbol": f"S{i}", "quantity": "1"} for i in range(MAX_EXTERNAL_ENTRIES)]
    assert len(parse_external_list(ok)) == MAX_EXTERNAL_ENTRIES
    with pytest.raises(ReconciliationInputError):
        parse_external_list([*ok, {"symbol": "ONEMORE", "quantity": "1"}])


def test_conflicting_currencies_on_duplicate_rows_are_rejected():
    with pytest.raises(ReconciliationInputError):
        parse_external_list([
            {"symbol": "A", "quantity": "1", "currency": "INR"},
            {"symbol": "A", "quantity": "1", "currency": "USD"},
        ])


@pytest.mark.parametrize(
    "qty,cost",
    [("-1", "0.5"), ("x", "0.5"), ("0", "-1"), ("0", "101"), ("NaN", "1"), ("0", "NaN")],
)
def test_invalid_tolerances(qty, cost):
    with pytest.raises(ReconciliationInputError):
        parse_tolerances(qty, cost)


def test_valid_tolerances():
    assert parse_tolerances("0", "0") == (Decimal("0"), Decimal("0"))
