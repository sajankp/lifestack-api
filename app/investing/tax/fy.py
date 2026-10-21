"""Indian financial-year helpers.

The Indian FY runs 1 April to 31 March. Labels are ``"YYYY-YY"`` where
``YYYY`` is the starting calendar year — so FY 2026-27 is 1 Apr 2026 to
31 Mar 2027.
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, date, datetime, time

_FY_LABEL_RE = re.compile(r"^(\d{4})-(\d{2})$")


def parse_fy_label(label: str) -> tuple[date, date]:
    """Parse an FY label into its inclusive start and end dates.

    Raises:
        ValueError: if the label is not ``YYYY-YY`` or the suffix does not
            match the start year.
    """
    match = _FY_LABEL_RE.match((label or "").strip())
    if not match:
        raise ValueError(f"Invalid FY label {label!r}; expected 'YYYY-YY'")
    start_year = int(match.group(1))
    suffix = int(match.group(2))
    if suffix != (start_year + 1) % 100:
        raise ValueError(
            f"Invalid FY label {label!r}; suffix must be the year after the start year"
        )
    return date(start_year, 4, 1), date(start_year + 1, 3, 31)


def fy_bounds(label: str) -> tuple[datetime, datetime]:
    """FY label -> (start, end) as timezone-aware UTC datetimes.

    The end is the last instant of 31 March so a sell timestamped at
    23:59:59 on 31 March is inside the FY.
    """
    start, end = parse_fy_label(label)
    return (
        datetime.combine(start, time.min, tzinfo=UTC),
        datetime.combine(end, time.max, tzinfo=UTC),
    )


def fy_label_for_date(value: date | datetime) -> str:
    """The FY label containing the given date."""
    if isinstance(value, datetime):
        value = value.date()
    start_year = value.year if value.month >= 4 else value.year - 1
    return f"{start_year}-{((start_year + 1) % 100):02d}"


def current_fy_label(now: datetime | None = None) -> str:
    """The FY label containing ``now`` (defaults to the current UTC time)."""
    if now is None:
        now = datetime.now(UTC)
    return fy_label_for_date(now)


def add_months(value: datetime, months: int) -> datetime:
    """Add calendar months, clamping the day to the target month's length.

    The 24-month holding-period test needs exact calendar arithmetic, not
    a days-based approximation — see ``regime.py``.
    """
    year, month = value.year, value.month + months
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def months_between(start: datetime, end: datetime) -> int:
    """Whole calendar months from ``start`` to ``end`` (truncated).

    Used only for display — the *classification* uses an exact calendar
    comparison, not this count.
    """
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return months
