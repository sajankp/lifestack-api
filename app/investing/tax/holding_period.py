"""The one long-term / short-term rule, shared by the filing engine and the what-if tools.

"More than N months" is decided on **calendar days**: a lot acquired on day D becomes
long-term on the day *after* D + N months (clamped to the month's last day). Time of day
never matters.

A calendar day depends on the timezone it is read in. The filing engine reads every
timestamp in India time -- the tax jurisdiction -- so a trade at 00:30 IST (stored as the
previous UTC day) counts for its Indian day. The what-if tools already receive datetimes
in the user's own timezone and read the date each value carries (``tz=None``).
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo

from app.investing.tax.fy import add_months

IST = ZoneInfo("Asia/Kolkata")


def as_day(value: datetime, tz: tzinfo | None = None) -> datetime:
    """Collapse a timezone-aware timestamp to midnight of its calendar day.

    With ``tz`` the date is read in that zone; with ``None`` it is the date the value
    carries in its own timezone. The result is a UTC-tagged carrier so every derived
    value is a plain comparable datetime. Naive datetimes are rejected: comparing naive
    and aware values raises ``TypeError`` deep in the engine, and silently assuming a
    zone would shift the tax year / FX month near midnight.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetimes must be timezone-aware")
    local = value.astimezone(tz) if tz is not None else value
    return datetime(local.year, local.month, local.day, tzinfo=UTC)


def long_term_mark_day(acquired_at: datetime, months: int, tz: tzinfo | None = None) -> datetime:
    """The N-month anniversary day. A sale must fall *after* this day to be long-term."""
    return add_months(as_day(acquired_at, tz), months)


def is_long_term_on(
    acquired_at: datetime, sold_at: datetime, months: int, tz: tzinfo | None = None
) -> bool:
    """Whether a lot held from ``acquired_at`` is long-term when sold at ``sold_at``."""
    return as_day(sold_at, tz) > long_term_mark_day(acquired_at, months, tz)
