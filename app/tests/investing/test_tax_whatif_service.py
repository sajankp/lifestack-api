"""Unit tests for the what-if service's pure input helpers (no database)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.investing.tax_whatif_service import (
    DEFAULT_TIMEZONE,
    WhatIfError,
    day_start,
    local_today,
    parse_decimal,
    parse_sale_date,
    resolve_zone,
)

IST = ZoneInfo("Asia/Kolkata")


class TestResolveZone:
    def test_defaults_to_indian_tax_zone(self):
        assert str(resolve_zone(None)) == DEFAULT_TIMEZONE == "Asia/Kolkata"
        assert str(resolve_zone("")) == "Asia/Kolkata"

    def test_invalid_zone_falls_back_instead_of_failing(self):
        assert str(resolve_zone("Mars/Olympus")) == "Asia/Kolkata"

    def test_valid_zone_is_used(self):
        assert str(resolve_zone("America/New_York")) == "America/New_York"


class TestParseSaleDate:
    def test_bare_date_is_a_local_calendar_day(self):
        parsed = parse_sale_date("2026-10-16", IST)
        assert parsed == datetime(2026, 10, 16, tzinfo=IST)
        assert parsed.tzinfo is not None

    def test_utc_instant_is_converted_to_the_local_day(self):
        # 20:00Z on 31 March is already 1 April in India (FY 2027-28).
        parsed = parse_sale_date("2027-03-31T20:00:00Z", IST)
        assert parsed.date() == date(2027, 4, 1)

    def test_offset_instant_is_converted(self):
        parsed = parse_sale_date("2026-10-16T23:30:00-05:00", IST)
        assert parsed.date() == date(2026, 10, 17)

    def test_naive_datetime_is_taken_as_local(self):
        assert parse_sale_date("2026-10-16T23:59:00", IST).date() == date(2026, 10, 16)

    @pytest.mark.parametrize(
        "bad", ["", "  ", "tomorrow", "2026-02-30", "1999-12-31", "2101-01-01"]
    )
    def test_invalid_dates_rejected(self, bad):
        with pytest.raises(WhatIfError) as exc:
            parse_sale_date(bad, IST)
        assert exc.value.code == "invalid_date"


class TestParseDecimal:
    def test_exact_decimal_strings(self):
        assert parse_decimal("qty", "0.00000001", allow_zero=False, max_places=8) == Decimal(
            "0.00000001"
        )
        assert parse_decimal("price", " 12.5 ", allow_zero=True) == Decimal("12.5")

    @pytest.mark.parametrize(
        ("raw", "allow_zero"),
        [
            ("abc", True),
            ("NaN", True),
            ("Infinity", True),
            ("-1", True),
            ("0", False),
            ("1" + "0" * 13, True),
            ("", True),
        ],
    )
    def test_rejects_non_finite_negative_zero_and_huge(self, raw, allow_zero):
        with pytest.raises(WhatIfError) as exc:
            parse_decimal("x", raw, allow_zero=allow_zero)
        assert exc.value.code == "invalid_number"

    def test_rejects_excess_precision(self):
        with pytest.raises(WhatIfError):
            parse_decimal("qty", "0.123456789", allow_zero=False, max_places=8)

    def test_accepts_exponent_notation_within_bounds(self):
        assert parse_decimal("qty", "1E+3", allow_zero=False, max_places=8) == Decimal("1000")


class TestLocalDay:
    def test_local_today_uses_the_zone_not_utc(self):
        instant = datetime(2026, 3, 31, 20, 0, tzinfo=UTC)
        assert local_today(IST, instant) == date(2026, 4, 1)
        assert local_today(ZoneInfo("UTC"), instant) == date(2026, 3, 31)

    def test_day_start_is_midnight_in_zone(self):
        assert day_start(date(2026, 4, 1), IST).utcoffset().total_seconds() == 5.5 * 3600
