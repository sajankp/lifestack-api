"""Shared recurrence-advance arithmetic (spec-053).

Used by both ``RecurringTodoRule`` and ``RecurringTransaction`` — previously
a private function in ``app.spending.service`` that ``app.application.
workflows`` reached across module boundaries to apply to todo rules too.
Moved here so both modules import from core instead of one depending on the
other's internals.
"""

import calendar
from datetime import date, timedelta
from enum import StrEnum
from typing import Literal

MonthlyModeLiteral = Literal["day_of_month", "last_day", "nth_weekday"]
OrdinalLiteral = Literal[-1, 1, 2, 3, 4]


class MonthlyRecurrenceMode(StrEnum):
    """Shared by RecurringTodoRule.monthly_mode and RecurringTransaction.monthly_mode."""

    day_of_month = "day_of_month"
    last_day = "last_day"
    nth_weekday = "nth_weekday"


def validate_recurrence_fields(
    frequency: str, monthly_mode: str, by_weekday: int | None, by_ordinal: int | None
) -> None:
    """Cross-field invariant mirrored by the DB CHECK constraints (spec-053).
    Shared by both RecurringTodoRuleCreate and RecurringTransactionCreate."""
    if monthly_mode != "day_of_month" and frequency != "monthly":
        raise ValueError("monthly_mode requires frequency='monthly'")
    has_nth_weekday_fields = by_weekday is not None and by_ordinal is not None
    if monthly_mode == "nth_weekday" and not has_nth_weekday_fields:
        raise ValueError("nth_weekday mode requires both by_weekday and by_ordinal")
    if monthly_mode != "nth_weekday" and (by_weekday is not None or by_ordinal is not None):
        raise ValueError("by_weekday/by_ordinal are only valid with monthly_mode='nth_weekday'")


def _nth_weekday_of_month(year: int, month: int, weekday: int, ordinal: int) -> date:
    """``weekday``: 0=Monday..6=Sunday (ISO, matches ``date.weekday()``).
    ``ordinal``: 1-4 = first-fourth occurrence in the month, -1 = last.

    Every month has at least 4 of each weekday (28-day February is the
    shortest), so ordinal 1-4 always resolves — no out-of-range fallback
    needed.
    """
    days_in_month = calendar.monthrange(year, month)[1]
    if ordinal == -1:
        last = date(year, month, days_in_month)
        offset = (last.weekday() - weekday) % 7
        return last - timedelta(days=offset)
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    day = 1 + offset + (ordinal - 1) * 7
    return date(year, month, day)


def _add_months(year: int, month: int, interval: int) -> tuple[int, int]:
    total = month - 1 + interval
    return year + total // 12, total % 12 + 1


def advance_due_date(
    current: date,
    frequency: str,
    interval: int,
    *,
    anchor_day: int | None = None,
    monthly_mode: str = "day_of_month",
    by_weekday: int | None = None,
    by_ordinal: int | None = None,
) -> date:
    """Advance ``current`` by one ``frequency``/``interval`` step.

    ``daily``/``weekly``/``yearly`` are unaffected by the monthly-mode
    arguments. For ``monthly``:

    - ``day_of_month`` (default): target day is ``min(anchor_day, days in
      target month)`` — clamped against the *anchor's* day, not the current
      date's day. This is the drift fix (spec-053): the old clamp-against-
      current-day behavior permanently loses a 29-31 anchor after the first
      short month it crosses. With ``anchor_day=None`` (only relevant for
      callers that don't have a rule's anchor handy), falls back to the old
      current-day-based behavior.
    - ``last_day``: always the final calendar day of the target month.
    - ``nth_weekday``: the ``by_ordinal``-th ``by_weekday`` of the target
      month; requires both ``by_weekday`` and ``by_ordinal``.
    """
    if frequency == "daily":
        return current + timedelta(days=interval)
    if frequency == "weekly":
        return current + timedelta(weeks=interval)
    if frequency == "yearly":
        try:
            return current.replace(year=current.year + interval)
        except ValueError:
            return date(current.year + interval, 2, 28)

    # monthly (default) and its modes
    year, month = _add_months(current.year, current.month, interval)

    if monthly_mode == "last_day":
        return date(year, month, calendar.monthrange(year, month)[1])

    if monthly_mode == "nth_weekday":
        if by_weekday is None or by_ordinal is None:
            raise ValueError("nth_weekday mode requires by_weekday and by_ordinal")
        return _nth_weekday_of_month(year, month, by_weekday, by_ordinal)

    target_day = anchor_day if anchor_day is not None else current.day
    day = min(target_day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def first_due_date(
    anchor_date: date,
    today: date,
    frequency: str,
    interval: int,
    *,
    monthly_mode: str = "day_of_month",
    by_weekday: int | None = None,
    by_ordinal: int | None = None,
) -> date:
    """First ``next_due_date`` for a rule created just now.

    ``anchor_date`` fixes the recurrence pattern (e.g. "the 1st of the month") and is
    often already in the past relative to ``today`` when a rule is created mid-cycle.
    Left as-is, that made a rule created seconds ago show as immediately overdue.
    Advance past any cycles that have already elapsed; a date on or after ``today`` is
    returned unchanged.
    """
    if interval <= 0:
        raise ValueError("interval must be greater than 0")
    due = anchor_date
    while due < today:
        due = advance_due_date(
            due,
            frequency,
            interval,
            anchor_day=anchor_date.day,
            monthly_mode=monthly_mode,
            by_weekday=by_weekday,
            by_ordinal=by_ordinal,
        )
    return due
