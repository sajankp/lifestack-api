"""Unit tests for spec-075's display-conversion FX cutoff rule: one rate per
calendar day, sourced from the *previous* day's close, applied uniformly to
historical and "current" views alike (no live/intraday refresh)."""

from datetime import UTC, datetime, timedelta, timezone

from app.core.currency import effective_display_as_of


def test_effective_display_as_of_defaults_to_yesterday_end_of_day():
    reference = datetime(2026, 7, 12, 9, 30, tzinfo=UTC)
    result = effective_display_as_of(reference)
    assert result == datetime(2026, 7, 11, 23, 59, 59, 999999, tzinfo=UTC)


def test_effective_display_as_of_shifts_back_one_calendar_day_regardless_of_time():
    # Even a reference at 00:00:01 still rolls back a full calendar day --
    # there is no partial-day "already saw today's close" carve-out.
    early = datetime(2026, 7, 12, 0, 0, 1, tzinfo=UTC)
    late = datetime(2026, 7, 12, 23, 59, 59, tzinfo=UTC)
    assert effective_display_as_of(early).date() == effective_display_as_of(late).date()


def test_effective_display_as_of_none_uses_current_time():
    now = datetime.now(UTC)
    result = effective_display_as_of()
    # The cutoff is always strictly in the past, on the previous calendar day.
    assert result < now
    assert result.date() < now.date()


def test_effective_display_as_of_normalizes_non_utc_reference_to_utc_calendar_day():
    # 2026-07-12 01:00 IST (UTC+5:30) is still 2026-07-11 in UTC. The cutoff
    # must be computed from the UTC calendar day (matching FxRate.as_of,
    # which is always UTC-anchored), not the reference's own offset.
    ist = timezone(timedelta(hours=5, minutes=30))
    reference = datetime(2026, 7, 12, 1, 0, tzinfo=ist)
    result = effective_display_as_of(reference)
    assert result == datetime(2026, 7, 10, 23, 59, 59, 999999, tzinfo=UTC)
