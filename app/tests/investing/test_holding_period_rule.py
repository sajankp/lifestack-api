"""Long-term classification: ONE calendar-day rule shared by the filing engine and what-if tools.

"More than N months" is decided on **calendar days**: a lot acquired on day D becomes
long-term on the day *after* D + N months. Time of day never matters, and the filing engine
reads each date in India time (the tax jurisdiction), so a trade at 00:30 IST that is stored
as the previous UTC day still counts for its Indian day.

These tests pin the filing engine (``gains.is_long_term``) and prove it agrees with the
what-if classifier on every boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.investing.tax.gains import is_long_term
from app.investing.tax.regime import FOREIGN_REGIME, INDIAN_REGIME
from app.investing.tax.simulation import _holding_period

IST = ZoneInfo("Asia/Kolkata")


def _ist(y, mo, d, h=0, mi=0) -> datetime:
    """An IST wall-clock time stored the way the database returns it (UTC)."""
    return datetime(y, mo, d, h, mi, tzinfo=IST).astimezone(UTC)


# --------------------------------------------------------------------------
# The filing engine decides on calendar days in India time
# --------------------------------------------------------------------------


def test_sale_on_the_anniversary_day_is_short_term_whatever_the_time():
    acquired = _ist(2025, 3, 10, 10, 0)
    # Later the same time-of-day AND later in the day: both still the 12-month day.
    assert is_long_term(acquired, _ist(2026, 3, 10, 15, 0), INDIAN_REGIME) is False
    assert is_long_term(acquired, _ist(2026, 3, 10, 23, 59), INDIAN_REGIME) is False


def test_sale_the_day_after_the_anniversary_is_long_term_even_at_midnight():
    acquired = _ist(2025, 3, 10, 22, 0)
    assert is_long_term(acquired, _ist(2026, 3, 11, 0, 5), INDIAN_REGIME) is True


def test_an_early_morning_ist_trade_counts_for_its_indian_day_not_the_utc_day():
    # 00:30 IST on 11 Mar is 19:00 UTC on 10 Mar. The Indian day is the 11th -> long-term.
    acquired = _ist(2025, 3, 10, 9, 0)
    sold = _ist(2026, 3, 11, 0, 30)
    assert sold.day == 10, "precondition: stored as the previous UTC day"
    assert is_long_term(acquired, sold, INDIAN_REGIME) is True


def test_an_early_morning_ist_purchase_counts_from_its_indian_day():
    # Bought 00:30 IST on 10 Mar (stored 9 Mar UTC). The holding starts on the 10th, so
    # a sale on 10 Mar 2026 is still on the anniversary (short), the 11th is long.
    acquired = _ist(2025, 3, 10, 0, 30)
    assert acquired.day == 9, "precondition: stored as the previous UTC day"
    assert is_long_term(acquired, _ist(2026, 3, 10, 12, 0), INDIAN_REGIME) is False
    assert is_long_term(acquired, _ist(2026, 3, 11, 12, 0), INDIAN_REGIME) is True


def test_foreign_regime_uses_the_same_day_rule_with_24_months():
    acquired = _ist(2024, 6, 15, 16, 0)
    assert is_long_term(acquired, _ist(2026, 6, 15, 18, 0), FOREIGN_REGIME) is False
    assert is_long_term(acquired, _ist(2026, 6, 16, 9, 0), FOREIGN_REGIME) is True


def test_month_end_acquisition_clamps_the_anniversary():
    # 31 Aug + 6 months clamps to 28 Feb (non-leap), so 1 Mar is the first long-term day
    # under a 6-month rule; checked with the 12-month rule: 29 Feb 2024 + 12m = 28 Feb 2025.
    acquired = _ist(2024, 2, 29, 11, 0)
    assert is_long_term(acquired, _ist(2025, 2, 28, 14, 0), INDIAN_REGIME) is False
    assert is_long_term(acquired, _ist(2025, 3, 1, 9, 0), INDIAN_REGIME) is True


# --------------------------------------------------------------------------
# Both engines give the same answer on every boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize("regime", [INDIAN_REGIME, FOREIGN_REGIME], ids=["india", "foreign"])
@pytest.mark.parametrize(
    "acquired_ist",
    [(2025, 1, 31, 0, 30), (2025, 3, 10, 10, 0), (2024, 2, 29, 23, 45), (2025, 8, 31, 12, 0)],
    ids=["jan31-early", "mar10-mid", "leap-day-late", "aug31-noon"],
)
def test_filing_engine_and_whatif_classifier_agree_on_every_boundary(regime, acquired_ist):
    acquired = _ist(*acquired_ist)
    months = regime.ltcg_holding_months
    anniversary = datetime(
        acquired.astimezone(IST).year + (acquired.astimezone(IST).month - 1 + months) // 12,
        (acquired.astimezone(IST).month - 1 + months) % 12 + 1,
        1,
        tzinfo=IST,
    )
    # Walk from 3 days before to 3 days after the anniversary month's start..end span.
    probe_start = anniversary - timedelta(days=3)
    for offset in range(0, 40):
        for hour, minute in ((0, 5), (9, 30), (23, 55)):
            day = probe_start + timedelta(days=offset)
            sold = datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)
            filing = is_long_term(acquired, sold.astimezone(UTC), regime)
            whatif = _holding_period(
                acquired.astimezone(IST), sold, regime
            ).is_long_term  # what-if callers pass user-timezone datetimes
            assert filing == whatif, (acquired_ist, sold.isoformat(), regime.key)
