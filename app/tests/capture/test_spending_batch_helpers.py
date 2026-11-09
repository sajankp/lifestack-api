"""Unit tests for the pure helpers behind idempotent spending writes (spec-101)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.capture.spending_batch import (
    CursorError,
    ResolvedSpend,
    count_results,
    decode_cursor,
    encode_cursor,
    filter_fingerprint,
    group_existing_by_natural_key,
    item_result,
    local_day_range_utc,
    natural_key,
    normalize_description,
    plan_natural_key_matches,
    same_item_payload,
    same_payload,
)

IST = ZoneInfo("Asia/Kolkata")
LA = ZoneInfo("America/Los_Angeles")


def _item(index, *, amount="50.00", description="Chai", account_id=1, day=date(2026, 7, 3)):
    return ResolvedSpend(
        index=index,
        amount=Decimal(amount),
        category=SimpleNamespace(id=1),
        account=SimpleNamespace(id=account_id),
        occurred_at=datetime(2026, 7, 3, 6, 30, tzinfo=UTC),
        local_day=day,
        description=description,
        tx_type=SimpleNamespace(value="expense"),
        source_ref=None,
        tags=[],
        allow_duplicate=False,
    )


@dataclass
class _Row:
    id: int
    account_id: int
    type: str
    amount: Decimal
    occurred_at: datetime
    description: str | None


def _row(id_, *, amount="50.00", description="Chai", account_id=1, occurred_at=None):
    return _Row(
        id=id_,
        account_id=account_id,
        type="expense",
        amount=Decimal(amount),
        occurred_at=occurred_at or datetime(2026, 7, 3, 6, 30, tzinfo=UTC),
        description=description,
    )


def test_normalize_description_collapses_case_and_whitespace():
    assert normalize_description("  Chai   AT  Stall ") == "chai at stall"
    assert normalize_description(None) == ""
    assert normalize_description("   ") == ""


def test_natural_key_ignores_amount_scale_and_description_noise():
    a = natural_key(1, "expense", Decimal("50"), date(2026, 7, 3), " Chai ")
    b = natural_key(1, "expense", Decimal("50.00"), date(2026, 7, 3), "chai")
    assert a == b
    assert a != natural_key(2, "expense", Decimal("50"), date(2026, 7, 3), "chai")
    assert a != natural_key(1, "income", Decimal("50"), date(2026, 7, 3), "chai")
    assert a != natural_key(1, "expense", Decimal("50.01"), date(2026, 7, 3), "chai")
    assert a != natural_key(1, "expense", Decimal("50"), date(2026, 7, 4), "chai")


@pytest.mark.parametrize(
    ("existing", "requested", "expected_matches"),
    [
        (0, 1, 0),
        (0, 2, 0),  # two genuine identical purchases are not collapsed
        (1, 1, 1),
        (1, 2, 1),  # ledger has one chai, batch has two -> match one, create one
        (2, 2, 2),
        (2, 3, 2),
        (3, 2, 2),  # never matches more items than were requested
    ],
)
def test_multiplicity_matches_at_most_what_the_ledger_holds(existing, requested, expected_matches):
    items = [_item(i) for i in range(requested)]
    rows = [_row(100 + n) for n in range(existing)]
    plan = plan_natural_key_matches(items, group_existing_by_natural_key(rows, IST))
    matched = [i for i, m in plan.items() if m is not None]
    assert len(matched) == expected_matches
    assert len(plan) - len(matched) == requested - expected_matches
    # Each existing row is claimed by at most one item.
    claimed = [plan[i].existing.id for i in matched]
    assert len(claimed) == len(set(claimed))


def test_multiplicity_is_tracked_per_key():
    items = [
        _item(0, description="Chai"),
        _item(1, description="Chai"),
        _item(2, description="Bus"),
        _item(3, description="Bus", amount="20.00"),
    ]
    rows = [_row(1, description="chai"), _row(2, description="BUS")]
    plan = plan_natural_key_matches(items, group_existing_by_natural_key(rows, IST))
    assert plan[0] is not None and plan[0].existing.id == 1
    assert plan[1] is None
    assert plan[2] is not None and plan[2].existing.id == 2
    assert plan[3] is None  # same description, different amount


def test_existing_rows_are_bucketed_by_local_day_not_utc_day():
    # 2026-07-03 20:00 UTC is 2026-07-04 01:30 in IST -> a different local day than
    # 2026-07-03 06:30 UTC (12:00 IST on the 3rd).
    late = _row(1, occurred_at=datetime(2026, 7, 3, 20, 0, tzinfo=UTC))
    grouped = group_existing_by_natural_key([late], IST)
    assert natural_key(1, "expense", Decimal("50"), date(2026, 7, 4), "chai") in grouped
    assert natural_key(1, "expense", Decimal("50"), date(2026, 7, 3), "chai") not in grouped


def test_same_payload_tolerates_cosmetic_differences_only():
    item = _item(0)
    existing = SimpleNamespace(
        amount=Decimal("50"),
        type="expense",
        account_id=1,
        description="  CHAI ",
        occurred_at=datetime(2026, 7, 3, 6, 30, tzinfo=UTC),
    )
    assert same_payload(existing, item, IST)
    existing.amount = Decimal("51")
    assert not same_payload(existing, item, IST)
    existing.amount = Decimal("50")
    existing.account_id = 2
    assert not same_payload(existing, item, IST)
    assert same_item_payload(_item(0), _item(1))
    assert not same_item_payload(_item(0), _item(1, amount="51.00"))


def test_local_day_range_is_inclusive_of_the_whole_end_day_in_ist():
    start, end = local_day_range_utc(date(2026, 7, 1), date(2026, 7, 3), IST)
    # IST = UTC+5:30: local midnight on the 1st is 18:30 UTC on 30 June.
    assert start == datetime(2026, 6, 30, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 7, 3, 18, 30, tzinfo=UTC)  # exclusive: start of 4 July IST


def test_local_day_range_negative_offset_and_dst_transition():
    start, end = local_day_range_utc(date(2026, 7, 1), date(2026, 7, 1), LA)
    assert (end - start).total_seconds() == 24 * 3600
    # 8 March 2026 is the US spring-forward day: only 23 local hours long.
    start, end = local_day_range_utc(date(2026, 3, 8), date(2026, 3, 8), LA)
    assert (end - start).total_seconds() == 23 * 3600
    # The following day starts exactly where this one ended: no gap, no overlap.
    next_start, _ = local_day_range_utc(date(2026, 3, 9), date(2026, 3, 9), LA)
    assert next_start == end


def test_count_results_maps_created_and_would_create_to_create():
    results = [
        item_result(0, "created"),
        item_result(1, "would_create"),
        item_result(2, "duplicate"),
        item_result(3, "possible_duplicate"),
        item_result(4, "invalid"),
        item_result(5, "not_created"),
    ]
    assert count_results(results) == {
        "create": 2,
        "duplicate": 1,
        "possible_duplicate": 1,
        "invalid": 1,
    }


def test_item_result_always_has_the_documented_keys():
    assert set(item_result(0, "created")) == {
        "index",
        "status",
        "match_reason",
        "existing_id",
        "errors",
        "warnings",
    }


def test_cursor_round_trip_and_default_offset():
    fp = filter_fingerprint(start="2026-07-01", end="2026-07-03")
    assert decode_cursor(None, fp) == 0
    assert decode_cursor("", fp) == 0
    assert decode_cursor(encode_cursor(25, fp), fp) == 25
    assert decode_cursor(encode_cursor(-5, fp), fp) == 0


def test_cursor_rejects_garbage_foreign_filters_and_wrong_types():
    fp = filter_fingerprint(start="2026-07-01")
    other = filter_fingerprint(start="2026-08-01")
    for bad in ("not-base64!!", "e30=", "bm90LWpzb24="):
        with pytest.raises(CursorError):
            decode_cursor(bad, fp)
    with pytest.raises(CursorError, match="does not match"):
        decode_cursor(encode_cursor(10, other), fp)
    with pytest.raises(CursorError):
        decode_cursor(12, fp)  # type: ignore[arg-type]


def test_filter_fingerprint_is_order_insensitive_and_value_sensitive():
    assert filter_fingerprint(a=1, b=2) == filter_fingerprint(b=2, a=1)
    assert filter_fingerprint(a=1) != filter_fingerprint(a=2)
