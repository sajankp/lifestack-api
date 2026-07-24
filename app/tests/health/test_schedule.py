import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.health.models import Medication, MedicationEvent
from app.health.schedule import (
    derive_slot_status,
    get_dose_slots_for_date,
    get_dose_slots_in_window,
    interval_next_due_date,
    is_scheduled_date,
)


def _medication(**overrides) -> Medication:
    defaults = {
        "id": 1,
        "public_id": uuid.uuid4(),
        "workspace_id": 1,
        "user_id": 1,
        "name": "Test Med",
        "frequency": "daily",
        "interval": 1,
        "days_of_week": None,
        "anchor_date": date(2026, 1, 1),
        "end_date": None,
        "timezone": "UTC",
        "times": ["09:00"],
        "is_active": True,
    }
    defaults.update(overrides)
    return Medication(**defaults)


def _event(**overrides) -> MedicationEvent:
    defaults = {
        "medication_id": 1,
        "workspace_id": 1,
        "user_id": 1,
        "scheduled_for": datetime(2026, 1, 3, 9, 0, tzinfo=UTC),
        "status": "taken",
        "taken_at": None,
    }
    defaults.update(overrides)
    return MedicationEvent(**defaults)


class TestDaily:
    def test_scheduled_every_day_by_default(self):
        med = _medication(frequency="daily", interval=1, anchor_date=date(2026, 1, 1))
        assert is_scheduled_date(med, date(2026, 1, 1)) is True
        assert is_scheduled_date(med, date(2026, 1, 5)) is True

    def test_every_n_days(self):
        med = _medication(frequency="daily", interval=3, anchor_date=date(2026, 1, 1))
        assert is_scheduled_date(med, date(2026, 1, 1)) is True
        assert is_scheduled_date(med, date(2026, 1, 3)) is False
        assert is_scheduled_date(med, date(2026, 1, 4)) is True

    def test_before_anchor_never_scheduled(self):
        med = _medication(frequency="daily", anchor_date=date(2026, 1, 10))
        assert is_scheduled_date(med, date(2026, 1, 5)) is False

    def test_after_end_date_never_scheduled(self):
        med = _medication(
            frequency="daily", anchor_date=date(2026, 1, 1), end_date=date(2026, 1, 10)
        )
        assert is_scheduled_date(med, date(2026, 1, 10)) is True
        assert is_scheduled_date(med, date(2026, 1, 11)) is False


class TestWeekly:
    def test_multi_weekday_every_week(self):
        # Anchor Thursday 2026-01-01; days_of_week Mon(0)/Wed(2)/Fri(4)
        med = _medication(
            frequency="weekly",
            interval=1,
            days_of_week=[0, 2, 4],
            anchor_date=date(2026, 1, 1),
        )
        assert is_scheduled_date(med, date(2026, 1, 5)) is True  # Monday
        assert is_scheduled_date(med, date(2026, 1, 7)) is True  # Wednesday
        assert is_scheduled_date(med, date(2026, 1, 9)) is True  # Friday
        assert is_scheduled_date(med, date(2026, 1, 6)) is False  # Tuesday

    def test_every_n_weeks(self):
        med = _medication(
            frequency="weekly",
            interval=2,
            days_of_week=[0],
            anchor_date=date(2026, 1, 5),  # Monday, week 0
        )
        assert is_scheduled_date(med, date(2026, 1, 5)) is True  # week 0
        assert is_scheduled_date(med, date(2026, 1, 12)) is False  # week 1, skipped
        assert is_scheduled_date(med, date(2026, 1, 19)) is True  # week 2


class TestMonthly:
    def test_day_of_month_with_clamping(self):
        # Anchor on the 31st — Feb has no 31st, so it clamps to the 28th.
        med = _medication(frequency="monthly", interval=1, anchor_date=date(2026, 1, 31))
        assert is_scheduled_date(med, date(2026, 1, 31)) is True
        assert is_scheduled_date(med, date(2026, 2, 28)) is True
        assert is_scheduled_date(med, date(2026, 3, 31)) is True

    def test_every_n_months(self):
        med = _medication(frequency="monthly", interval=3, anchor_date=date(2026, 1, 15))
        assert is_scheduled_date(med, date(2026, 1, 15)) is True
        assert is_scheduled_date(med, date(2026, 2, 15)) is False
        assert is_scheduled_date(med, date(2026, 4, 15)) is True


class TestGetDoseSlotsForDate:
    def test_multiple_times_sorted(self):
        med = _medication(times=["21:00", "09:00"], anchor_date=date(2026, 1, 1))
        slots = get_dose_slots_for_date(med, date(2026, 1, 1))
        assert len(slots) == 2
        assert slots[0] < slots[1]

    def test_inactive_medication_has_no_slots(self):
        med = _medication(is_active=False, anchor_date=date(2026, 1, 1))
        assert get_dose_slots_for_date(med, date(2026, 1, 1)) == []

    def test_unscheduled_day_has_no_slots(self):
        med = _medication(frequency="daily", interval=5, anchor_date=date(2026, 1, 1))
        assert get_dose_slots_for_date(med, date(2026, 1, 2)) == []

    def test_timezone_conversion(self):
        med = _medication(timezone="Asia/Kolkata", times=["09:00"], anchor_date=date(2026, 1, 1))
        slots = get_dose_slots_for_date(med, date(2026, 1, 1))
        assert len(slots) == 1
        # 09:00 IST == 03:30 UTC
        assert slots[0] == datetime(2026, 1, 1, 3, 30, tzinfo=UTC)


class TestGetDoseSlotsInWindow:
    def test_finds_slot_in_short_window(self):
        med = _medication(times=["09:00"], anchor_date=date(2026, 1, 1))
        target = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        slots = get_dose_slots_in_window(
            med, target - timedelta(minutes=5), target + timedelta(minutes=5)
        )
        assert slots == [target]

    def test_empty_when_no_slot_in_window(self):
        med = _medication(times=["09:00"], anchor_date=date(2026, 1, 1))
        window_start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        window_end = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)
        assert get_dose_slots_in_window(med, window_start, window_end) == []


class TestDeriveSlotStatus:
    def test_event_status_wins(self):
        now = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        scheduled = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert derive_slot_status(scheduled, "taken", now, grace_hours=4) == "taken"

    def test_future_slot_is_pending(self):
        now = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        scheduled = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        assert derive_slot_status(scheduled, None, now, grace_hours=4) == "pending"

    def test_within_grace_window_is_still_pending(self):
        now = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
        scheduled = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)  # 2h ago, grace=4h
        assert derive_slot_status(scheduled, None, now, grace_hours=4) == "pending"

    def test_past_grace_window_is_missed(self):
        now = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
        scheduled = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)  # 5h ago, grace=4h
        assert derive_slot_status(scheduled, None, now, grace_hours=4) == "missed"

    def test_late_log_flips_missed_to_taken(self):
        now = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
        scheduled = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert derive_slot_status(scheduled, "taken", now, grace_hours=4) == "taken"


class TestIntervalNextDueDate:
    """spec-092: interval_from_last_dose re-anchors off the actual last dose."""

    tz = ZoneInfo("UTC")

    def test_no_event_first_dose_on_anchor(self):
        med = _medication(
            schedule_mode="interval_from_last_dose", interval=2, anchor_date=date(2026, 1, 1)
        )
        assert interval_next_due_date(med, None, self.tz) == date(2026, 1, 1)

    def test_taken_reanchors_off_taken_at(self):
        # Every 2 days; dose was DUE on the 3rd but actually taken on the 4th.
        # Next due must be 4th + 2 = 6th, NOT 3rd + 2 = 5th (that would be fixed).
        med = _medication(schedule_mode="interval_from_last_dose", interval=2)
        event = _event(
            status="taken",
            scheduled_for=datetime(2026, 1, 3, 9, 0, tzinfo=UTC),
            taken_at=datetime(2026, 1, 4, 8, 0, tzinfo=UTC),
        )
        assert interval_next_due_date(med, event, self.tz) == date(2026, 1, 6)

    def test_taken_without_taken_at_falls_back_to_slot(self):
        med = _medication(schedule_mode="interval_from_last_dose", interval=2)
        event = _event(
            status="taken",
            scheduled_for=datetime(2026, 1, 3, 9, 0, tzinfo=UTC),
            taken_at=None,
        )
        assert interval_next_due_date(med, event, self.tz) == date(2026, 1, 5)

    def test_skipped_advances_off_slot_date(self):
        # A skip has no intake, so cadence continues off the slot it answered.
        med = _medication(schedule_mode="interval_from_last_dose", interval=2)
        event = _event(
            status="skipped",
            scheduled_for=datetime(2026, 1, 3, 9, 0, tzinfo=UTC),
            taken_at=None,
        )
        assert interval_next_due_date(med, event, self.tz) == date(2026, 1, 5)

    def test_end_date_cutoff_returns_none(self):
        med = _medication(
            schedule_mode="interval_from_last_dose", interval=2, end_date=date(2026, 1, 4)
        )
        event = _event(
            status="taken",
            scheduled_for=datetime(2026, 1, 3, 9, 0, tzinfo=UTC),
            taken_at=datetime(2026, 1, 4, 8, 0, tzinfo=UTC),
        )
        assert interval_next_due_date(med, event, self.tz) is None

    def test_timezone_boundary_uses_local_date(self):
        # taken_at 2026-01-04 22:00 UTC == 2026-01-05 03:30 IST → +2 = Jan 7.
        med = _medication(
            schedule_mode="interval_from_last_dose", interval=2, timezone="Asia/Kolkata"
        )
        event = _event(
            status="taken",
            scheduled_for=datetime(2026, 1, 3, 3, 30, tzinfo=UTC),
            taken_at=datetime(2026, 1, 4, 22, 0, tzinfo=UTC),
        )
        assert interval_next_due_date(med, event, ZoneInfo("Asia/Kolkata")) == date(2026, 1, 7)


@pytest.mark.parametrize("frequency", ["daily", "monthly"])
def test_days_of_week_ignored_outside_weekly(frequency):
    # Model-level: days_of_week is only meaningful for weekly; schedule
    # arithmetic for other frequencies doesn't consult it at all.
    med = _medication(frequency=frequency, days_of_week=[0, 1], anchor_date=date(2026, 1, 1))
    assert is_scheduled_date(med, date(2026, 1, 1)) is True
