"""Unit tests for market-anchored trade-date helpers (IST-morning chain, spec-089).

Regression suite for the one-trading-day-stale closes bug: the bhavcopy and
closing-price jobs computed trade dates from the UTC 'previous weekday', so
at 04:15 IST the just-finished NSE session and at 23:00 UTC the just-settled
US session were both skipped and closes were ingested ~36h late, making
'latest completed market close' caches, net worth snapshots, insights and the
morning briefing run one trading day behind.
"""

from datetime import UTC, date, datetime

from app.investing.service import (
    _latest_completed_market_close,
    _latest_completed_nse_trading_day,
)


def test_nse_friday_0415_ist_gets_fridays_session():
    # Fri 2026-10-16 22:45 UTC == Sat 2026-10-17 04:15 IST.
    # The finished NSE session is Friday Oct 16.
    now = datetime(2026, 10, 16, 22, 45, tzinfo=UTC)
    assert _latest_completed_nse_trading_day(now) == date(2026, 10, 16)


def test_nse_monday_early_ist_gets_fridays_session():
    # Mon 2026-10-19 07:00 UTC == Mon 12:30 IST: Monday's session is still
    # running (closes 15:30 IST), so Friday must be used.
    now = datetime(2026, 10, 19, 7, 0, tzinfo=UTC)
    assert _latest_completed_nse_trading_day(now) == date(2026, 10, 16)


def test_nse_monday_late_ist_gets_mondays_session():
    # Mon 2026-10-19 22:45 UTC == Tue 04:15 IST: Monday's session finished.
    now = datetime(2026, 10, 19, 22, 45, tzinfo=UTC)
    assert _latest_completed_nse_trading_day(now) == date(2026, 10, 19)


def test_nse_weekend_gets_fridays_session():
    # Sat 2026-10-17 22:45 UTC == Sun 04:15 IST -> Friday Oct 16.
    now = datetime(2026, 10, 17, 22, 45, tzinfo=UTC)
    assert _latest_completed_nse_trading_day(now) == date(2026, 10, 16)


def test_market_close_friday_2300_utc_gets_same_day():
    # The 23:00 UTC run (spec-089) is past the worst-case winter US settle;
    # Friday's own close must be considered completed.
    now = datetime(2026, 10, 16, 23, 0, tzinfo=UTC)
    assert _latest_completed_market_close(now) == date(2026, 10, 16)


def test_market_close_monday_2300_utc_gets_same_day():
    now = datetime(2026, 10, 19, 23, 0, tzinfo=UTC)
    assert _latest_completed_market_close(now) == date(2026, 10, 19)


def test_market_close_saturday_gets_friday():
    now = datetime(2026, 10, 17, 23, 0, tzinfo=UTC)
    assert _latest_completed_market_close(now) == date(2026, 10, 16)


def test_market_close_sunday_gets_friday():
    now = datetime(2026, 10, 18, 23, 0, tzinfo=UTC)
    assert _latest_completed_market_close(now) == date(2026, 10, 16)
