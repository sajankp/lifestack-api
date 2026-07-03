import calendar
from datetime import date, timedelta

import pytest

from app.core.recurrence import advance_due_date


def _old_advance_due_date(current: date, frequency: str, interval: int) -> date:
    """The pre-spec-053 implementation (app.spending.service._advance_due_date,
    now removed) — kept here only as a regression reference for the
    byte-identical-for-day<=28 guarantee."""
    if frequency == "daily":
        return current + timedelta(days=interval)
    if frequency == "weekly":
        return current + timedelta(weeks=interval)
    if frequency == "yearly":
        try:
            return current.replace(year=current.year + interval)
        except ValueError:
            return date(current.year + interval, 2, 28)
    month = current.month - 1 + interval
    year = current.year + month // 12
    month = month % 12 + 1
    day = min(current.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


class TestDayOfMonthDriftFix:
    def test_anchor_31_advances_without_permanent_drift(self):
        current = date(2026, 1, 31)
        expected = [date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]
        for exp in expected:
            current = advance_due_date(current, "monthly", 1, anchor_day=31)
            assert current == exp

    def test_anchor_day_le_28_matches_old_behavior_across_24_advances(self):
        anchor_day = 15
        new_current = date(2026, 1, 15)
        old_current = date(2026, 1, 15)
        for _ in range(24):
            new_current = advance_due_date(new_current, "monthly", 1, anchor_day=anchor_day)
            old_current = _old_advance_due_date(old_current, "monthly", 1)
            assert new_current == old_current

    def test_anchor_day_none_falls_back_to_current_day(self):
        # No anchor supplied: behaves like the old current-day-based clamp.
        current = date(2026, 1, 31)
        result = advance_due_date(current, "monthly", 1)
        assert result == date(2026, 2, 28)


class TestLastDayMode:
    def test_last_day_tracks_month_end(self):
        current = date(2026, 1, 31)
        expected = [date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]
        for exp in expected:
            current = advance_due_date(current, "monthly", 1, monthly_mode="last_day")
            assert current == exp

    def test_last_day_leap_year_february(self):
        current = date(2028, 1, 31)
        result = advance_due_date(current, "monthly", 1, monthly_mode="last_day")
        assert result == date(2028, 2, 29)


class TestNthWeekdayMode:
    def test_first_friday(self):
        current = date(2026, 7, 3)
        first = advance_due_date(
            current, "monthly", 1, monthly_mode="nth_weekday", by_weekday=4, by_ordinal=1
        )
        assert first == date(2026, 8, 7)
        second = advance_due_date(
            first, "monthly", 1, monthly_mode="nth_weekday", by_weekday=4, by_ordinal=1
        )
        assert second == date(2026, 9, 4)

    def test_last_sunday_crosses_year_boundary(self):
        current = date(2026, 12, 27)  # a Sunday
        result = advance_due_date(
            current, "monthly", 1, monthly_mode="nth_weekday", by_weekday=6, by_ordinal=-1
        )
        assert result == date(2027, 1, 31)

    def test_interval_composition(self):
        current = date(2026, 7, 3)
        result = advance_due_date(
            current, "monthly", 2, monthly_mode="nth_weekday", by_weekday=4, by_ordinal=1
        )
        assert result == date(2026, 9, 4)

    def test_missing_fields_raises(self):
        with pytest.raises(ValueError, match="requires by_weekday and by_ordinal"):
            advance_due_date(date(2026, 7, 3), "monthly", 1, monthly_mode="nth_weekday")


class TestNonMonthlyFrequenciesUnaffected:
    def test_daily_ignores_monthly_mode(self):
        result = advance_due_date(
            date(2026, 1, 1), "daily", 3, monthly_mode="last_day", by_weekday=4, by_ordinal=1
        )
        assert result == date(2026, 1, 4)

    def test_weekly_ignores_monthly_mode(self):
        result = advance_due_date(date(2026, 1, 1), "weekly", 2, monthly_mode="last_day")
        assert result == date(2026, 1, 15)

    def test_yearly_leap_day_anchor_falls_back_to_feb_28(self):
        result = advance_due_date(date(2028, 2, 29), "yearly", 1)
        assert result == date(2029, 2, 28)
