"""Medication dose-schedule arithmetic (spec-069).

Dose slots are never stored — they're derived on read from
(frequency, interval, anchor_date, days_of_week, times, timezone, end_date).
Reuses the RecurringTodoRule vocabulary (daily/weekly/monthly + interval);
the one extension is `days_of_week`, which `core/recurrence.py::
advance_due_date` doesn't model (it handles single-cadence weekdays only via
`nth_weekday`, a different concept). Monthly day-of-month clamping mirrors
`advance_due_date`'s anchor-day clamp (spec-053 drift fix): the target day is
`min(anchor_date.day, days_in_target_month)`, not the current day.
"""

import calendar
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.health.models import Medication

DoseSlotStatus = str  # "pending" | "taken" | "skipped" | "missed"


def is_scheduled_date(medication: Medication, target: date) -> bool:
    """Whether `target` (a calendar date) has a dose scheduled per the
    medication's recurrence rule, ignoring end_date/anchor bounds checks
    that callers may want to apply separately (both are also checked here
    for convenience)."""
    if target < medication.anchor_date:
        return False
    if medication.end_date is not None and target > medication.end_date:
        return False

    if medication.frequency == "daily":
        delta_days = (target - medication.anchor_date).days
        return delta_days % medication.interval == 0

    if medication.frequency == "weekly":
        days_of_week = medication.days_of_week or []
        if target.weekday() not in days_of_week:
            return False
        anchor_week_start = medication.anchor_date - timedelta(
            days=medication.anchor_date.weekday()
        )
        target_week_start = target - timedelta(days=target.weekday())
        delta_weeks = (target_week_start - anchor_week_start).days // 7
        return delta_weeks >= 0 and delta_weeks % medication.interval == 0

    if medication.frequency == "monthly":
        delta_months = (target.year - medication.anchor_date.year) * 12 + (
            target.month - medication.anchor_date.month
        )
        if delta_months < 0 or delta_months % medication.interval != 0:
            return False
        days_in_month = calendar.monthrange(target.year, target.month)[1]
        target_day = min(medication.anchor_date.day, days_in_month)
        return target.day == target_day

    return False


def get_dose_slots_for_date(medication: Medication, target: date) -> list[datetime]:
    """Dose slot datetimes (UTC-aware) on `target`, one per `times` entry,
    interpreted in the medication's timezone. Empty if `target` isn't a
    scheduled day or the medication is inactive."""
    if not medication.is_active or not is_scheduled_date(medication, target):
        return []
    tz = ZoneInfo(medication.timezone)
    slots = []
    for time_str in medication.times:
        hour, minute = (int(part) for part in time_str.split(":"))
        local_dt = datetime(target.year, target.month, target.day, hour, minute, tzinfo=tz)
        slots.append(local_dt.astimezone(UTC))
    return sorted(slots)


def get_dose_slots_in_window(
    medication: Medication, window_start: datetime, window_end: datetime
) -> list[datetime]:
    """Dose slots falling within [window_start, window_end] — used by
    medication_reminder_job. Scans every calendar date (in the medication's
    timezone) touched by the window, since a slot's local date can differ
    from window_start.date() near timezone/date boundaries."""
    tz = ZoneInfo(medication.timezone)
    start_local_date = window_start.astimezone(tz).date()
    end_local_date = window_end.astimezone(tz).date()
    slots: list[datetime] = []
    current = start_local_date
    while current <= end_local_date:
        for slot in get_dose_slots_for_date(medication, current):
            if window_start <= slot <= window_end:
                slots.append(slot)
        current += timedelta(days=1)
    return sorted(slots)


def derive_slot_status(
    scheduled_for: datetime,
    event_status: str | None,
    now: datetime,
    grace_hours: int,
) -> DoseSlotStatus:
    """A slot's status is computed, never stored (spec-069 §A): logging late
    flips a missed slot to taken/skipped with no reconciliation job needed."""
    if event_status is not None:
        return event_status
    if scheduled_for > now:
        return "pending"
    if now - scheduled_for > timedelta(hours=grace_hours):
        return "missed"
    return "pending"
