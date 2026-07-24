import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.audit import AuditLogger, snapshot_columns
from app.core.exceptions import NotFoundError, ValidationError
from app.core.pagination import DEFAULT_LIMIT
from app.health.models import Medication, MedicationEvent, WeightEntry
from app.health.repository import (
    MedicationEventRepository,
    MedicationRepository,
    WeightEntryRepository,
)
from app.health.schedule import (
    derive_slot_status,
    get_dose_slots_for_date,
    interval_next_due_date,
    slot_datetimes_on,
)
from app.health.schemas import (
    DoseSlot,
    MedicationCreate,
    MedicationEventUpsert,
    MedicationResponse,
    MedicationUpdate,
    WeightEntryCreate,
    WeightEntryResponse,
    WeightTrendResponse,
)

_MEDICATION_AUDIT_FIELDS = (
    "name",
    "dose_text",
    "refill_note",
    "frequency",
    "interval",
    "days_of_week",
    "anchor_date",
    "end_date",
    "timezone",
    "times",
    "is_active",
    "reminders_enabled",
)

_WEIGHT_AUDIT_FIELDS = ("measured_at", "weight_kg", "note")

_EVENT_AUDIT_FIELDS = ("scheduled_for", "status", "note", "taken_at")

_INTERVAL_MODE = "interval_from_last_dose"


def _snapshot_medication(med: Medication) -> dict:
    data = snapshot_columns(med, _MEDICATION_AUDIT_FIELDS)
    for field in ("anchor_date", "end_date"):
        if data.get(field) is not None:
            data[field] = data[field].isoformat()
    return data


def _snapshot_weight(entry: WeightEntry) -> dict:
    data = snapshot_columns(entry, _WEIGHT_AUDIT_FIELDS)
    data["measured_at"] = data["measured_at"].isoformat()
    data["weight_kg"] = str(data["weight_kg"])
    return data


def _snapshot_event(event: MedicationEvent) -> dict:
    data = snapshot_columns(event, _EVENT_AUDIT_FIELDS)
    data["scheduled_for"] = data["scheduled_for"].isoformat()
    if data.get("taken_at") is not None:
        data["taken_at"] = data["taken_at"].isoformat()
    return data


class HealthService:
    def __init__(
        self,
        medication_repo: MedicationRepository,
        event_repo: MedicationEventRepository,
        weight_repo: WeightEntryRepository,
    ):
        self.medication_repo = medication_repo
        self.event_repo = event_repo
        self.weight_repo = weight_repo

    # -- Medications ---------------------------------------------------

    async def list_medications(
        self,
        workspace_id: int,
        is_active: bool | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[list[MedicationResponse], int]:
        items, total = await self.medication_repo.get_all(workspace_id, is_active, limit, offset)
        event_counts = await self.medication_repo.get_event_counts([m.id for m in items])
        return [self._to_response(m, event_count=event_counts.get(m.id, 0)) for m in items], total

    async def get_medication(self, workspace_id: int, public_id: uuid.UUID) -> Medication:
        med = await self.medication_repo.get_by_public_id(workspace_id, public_id)
        if not med:
            raise NotFoundError(detail=f"Medication with id {public_id} not found")
        return med

    async def get_medication_response(
        self, workspace_id: int, public_id: uuid.UUID
    ) -> MedicationResponse:
        med = await self.get_medication(workspace_id, public_id)
        event_count = await self.medication_repo.get_event_count(med.id)
        return self._to_response(med, event_count=event_count)

    @staticmethod
    def _to_response(med: Medication, *, event_count: int) -> MedicationResponse:
        response = MedicationResponse.model_validate(med)
        response.event_count = event_count
        return response

    async def create_medication(
        self,
        user_id: int,
        workspace_id: int,
        payload: MedicationCreate,
        audit_logger: AuditLogger | None = None,
    ) -> Medication:
        med = Medication(user_id=user_id, workspace_id=workspace_id, **payload.model_dump())
        med = await self.medication_repo.create(med)
        if audit_logger:
            after_snap = _snapshot_medication(med)
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="create",
                module="health",
                entity_type="medication",
                entity_id=med.id,
                details={
                    "entity_public_id": str(med.public_id),
                    "before": None,
                    "after": after_snap,
                    "changed_fields": list(after_snap.keys()),
                },
            )
        return med

    async def update_medication(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        payload: MedicationUpdate,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> Medication:
        med = await self.get_medication(workspace_id, public_id)
        before_snap = _snapshot_medication(med)
        update_data = payload.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(med, key, value)
        if med.frequency != "weekly":
            med.days_of_week = None
        elif med.frequency == "weekly" and not med.days_of_week:
            raise ValidationError(detail="days_of_week is required when frequency is weekly")
        # spec-092: interval_from_last_dose is daily-only — validate against the
        # medication's resulting state (payload may change only one of the two).
        if med.schedule_mode == _INTERVAL_MODE and med.frequency != "daily":
            raise ValidationError(detail="interval_from_last_dose requires frequency 'daily'")
        if med.end_date is not None and med.anchor_date > med.end_date:
            raise ValidationError(detail="anchor_date cannot be after end_date")
        med.updated_at = datetime.now(UTC)
        med = await self.medication_repo.save(med)

        after_snap = _snapshot_medication(med)
        changed_fields = [k for k in before_snap if before_snap[k] != after_snap[k]]
        if audit_logger and actor_id is not None:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="update",
                module="health",
                entity_type="medication",
                entity_id=med.id,
                details={
                    "entity_public_id": str(med.public_id),
                    "before": before_snap,
                    "after": after_snap,
                    "changed_fields": changed_fields,
                },
            )
        return med

    async def delete_medication(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> int:
        """Deletes the medication (events cascade at the DB level). Returns
        the event count that was deleted, for the confirm-surface count."""
        med = await self.get_medication(workspace_id, public_id)
        before_snap = _snapshot_medication(med)
        event_count = await self.medication_repo.get_event_count(med.id)
        await self.medication_repo.delete(med)
        if audit_logger and actor_id is not None:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="delete",
                module="health",
                entity_type="medication",
                entity_id=med.id,
                details={
                    "entity_public_id": str(med.public_id),
                    "before": before_snap,
                    "after": None,
                    "changed_fields": [],
                    "events_deleted": event_count,
                },
            )
        return event_count

    # -- Schedule / dose events -----------------------------------------

    def _build_slot(
        self, med: Medication, scheduled_for: datetime, event: MedicationEvent | None, now: datetime
    ) -> DoseSlot:
        status = derive_slot_status(
            scheduled_for,
            event.status if event else None,
            now,
            settings.HEALTH_DOSE_GRACE_HOURS,
        )
        return DoseSlot(
            medication_public_id=med.public_id,
            medication_name=med.name,
            dose_text=med.dose_text,
            scheduled_for=scheduled_for,
            status=status,
            event_public_id=event.public_id if event else None,
            note=event.note if event else None,
            taken_at=event.taken_at if event else None,
        )

    def _interval_due_datetimes(
        self, med: Medication, latest_event: MedicationEvent | None, target: date
    ) -> list[datetime]:
        """The slot datetimes an interval_from_last_dose medication has on
        `target` — non-empty only when its computed next-due date is `target`."""
        due = interval_next_due_date(med, latest_event, ZoneInfo(med.timezone))
        if due is None or due != target:
            return []
        return slot_datetimes_on(med, target)

    async def get_schedule(self, workspace_id: int, target: date) -> list[DoseSlot]:
        medications = await self.medication_repo.get_active(workspace_id)
        if not medications:
            return []
        med_ids = [m.id for m in medications]
        day_start = datetime.combine(target, datetime.min.time(), tzinfo=UTC) - timedelta(days=1)
        day_end = datetime.combine(target, datetime.min.time(), tzinfo=UTC) + timedelta(days=2)
        events = await self.event_repo.get_for_medications_between(med_ids, day_start, day_end)
        events_by_slot: dict[tuple[int, datetime], MedicationEvent] = {
            (e.medication_id, e.scheduled_for): e for e in events
        }
        interval_ids = [m.id for m in medications if m.schedule_mode == _INTERVAL_MODE]
        latest_events = await self.event_repo.get_latest_events_for_medications(interval_ids)
        now = datetime.now(UTC)
        slots: list[DoseSlot] = []
        for med in medications:
            if med.schedule_mode == _INTERVAL_MODE:
                scheduled_datetimes = self._interval_due_datetimes(
                    med, latest_events.get(med.id), target
                )
            else:
                scheduled_datetimes = get_dose_slots_for_date(med, target)
            for scheduled_for in scheduled_datetimes:
                event = events_by_slot.get((med.id, scheduled_for))
                slots.append(self._build_slot(med, scheduled_for, event, now))
        slots.sort(key=lambda s: s.scheduled_for)
        return slots

    async def get_overdue_slots(self, workspace_id: int, lookback_days: int) -> list[DoseSlot]:
        """Unanswered, past-grace ("missed") dose slots across active meds within
        the lookback window (spec-092), newest-first — powers the Catch-up
        section. Fixed meds: scan each date in the window. Interval meds: the
        single live slot iff it is missed and within the window."""
        medications = await self.medication_repo.get_active(workspace_id)
        if not medications:
            return []
        med_ids = [m.id for m in medications]
        now = datetime.now(UTC)
        today = now.date()
        window_start_date = today - timedelta(days=lookback_days)
        window_start = datetime.combine(window_start_date, datetime.min.time(), tzinfo=UTC)
        events = await self.event_repo.get_for_medications_between(med_ids, window_start, now)
        events_by_slot: dict[tuple[int, datetime], MedicationEvent] = {
            (e.medication_id, e.scheduled_for): e for e in events
        }
        interval_ids = [m.id for m in medications if m.schedule_mode == _INTERVAL_MODE]
        latest_events = await self.event_repo.get_latest_events_for_medications(interval_ids)

        slots: list[DoseSlot] = []
        for med in medications:
            candidate_datetimes: list[datetime] = []
            if med.schedule_mode == _INTERVAL_MODE:
                due = interval_next_due_date(med, latest_events.get(med.id), ZoneInfo(med.timezone))
                if due is not None and window_start_date <= due <= today:
                    candidate_datetimes = slot_datetimes_on(med, due)
            else:
                current = window_start_date
                while current <= today:
                    candidate_datetimes.extend(get_dose_slots_for_date(med, current))
                    current += timedelta(days=1)
            for scheduled_for in candidate_datetimes:
                if scheduled_for < window_start or scheduled_for > now:
                    continue
                event = events_by_slot.get((med.id, scheduled_for))
                slot = self._build_slot(med, scheduled_for, event, now)
                if slot.status == "missed":
                    slots.append(slot)
        slots.sort(key=lambda s: s.scheduled_for, reverse=True)
        return slots

    async def upsert_event(
        self,
        user_id: int,
        workspace_id: int,
        medication_public_id: uuid.UUID,
        payload: MedicationEventUpsert,
        audit_logger: AuditLogger | None = None,
    ) -> MedicationEvent:
        med = await self.get_medication(workspace_id, medication_public_id)
        # spec-092: "taken" carries an actual intake moment (honest late/back-dated
        # log; interval_from_last_dose re-anchors off it), defaulting to now when the
        # caller omits it. "skipped" has no intake, so taken_at is always cleared.
        taken_at = (payload.taken_at or datetime.now(UTC)) if payload.status == "taken" else None
        existing = await self.event_repo.get_by_slot(med.id, payload.scheduled_for)
        if existing:
            before_snap = _snapshot_event(existing)
            existing.status = payload.status
            existing.note = payload.note
            existing.taken_at = taken_at
            existing.logged_at = datetime.now(UTC)
            existing.updated_at = datetime.now(UTC)
            event = await self.event_repo.save(existing)
            action = "update"
            before = before_snap
        else:
            event = MedicationEvent(
                workspace_id=workspace_id,
                user_id=user_id,
                medication_id=med.id,
                scheduled_for=payload.scheduled_for,
                status=payload.status,
                note=payload.note,
                taken_at=taken_at,
            )
            event = await self.event_repo.create(event)
            action = "create"
            before = None

        if audit_logger:
            after_snap = _snapshot_event(event)
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action=action,
                module="health",
                entity_type="medication_event",
                entity_id=event.id,
                details={
                    "entity_public_id": str(event.public_id),
                    "before": before,
                    "after": after_snap,
                    "changed_fields": list(after_snap.keys())
                    if before is None
                    else [k for k in after_snap if before.get(k) != after_snap[k]],
                },
            )
        return event

    # -- Weight -----------------------------------------------------------

    async def list_weight_entries(
        self,
        workspace_id: int,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[list[WeightEntryResponse], int]:
        items, total = await self.weight_repo.get_range(workspace_id, start, end, limit, offset)
        return [WeightEntryResponse.model_validate(w) for w in items], total

    async def create_weight_entry(
        self,
        user_id: int,
        workspace_id: int,
        payload: WeightEntryCreate,
        audit_logger: AuditLogger | None = None,
    ) -> WeightEntry:
        entry = WeightEntry(user_id=user_id, workspace_id=workspace_id, **payload.model_dump())
        entry = await self.weight_repo.create(entry)
        if audit_logger:
            after_snap = _snapshot_weight(entry)
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="create",
                module="health",
                entity_type="weight_entry",
                entity_id=entry.id,
                details={
                    "entity_public_id": str(entry.public_id),
                    "before": None,
                    "after": after_snap,
                    "changed_fields": list(after_snap.keys()),
                },
            )
        return entry

    async def delete_weight_entry(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        result = await self.weight_repo.get_by_public_id(workspace_id, public_id)
        if not result:
            raise NotFoundError(detail=f"Weight entry with id {public_id} not found")
        before_snap = _snapshot_weight(result)
        await self.weight_repo.delete(result)
        if audit_logger and actor_id is not None:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="delete",
                module="health",
                entity_type="weight_entry",
                entity_id=result.id,
                details={
                    "entity_public_id": str(result.public_id),
                    "before": before_snap,
                    "after": None,
                    "changed_fields": [],
                },
            )

    async def get_weight_trend(self, workspace_id: int, days: int = 30) -> WeightTrendResponse:
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        items, _total = await self.weight_repo.get_range(
            workspace_id, start, end, limit=1000, offset=0
        )
        entries = [WeightEntryResponse.model_validate(w) for w in items]
        if not entries:
            return WeightTrendResponse(
                entries=[],
                latest_kg=None,
                delta_7d_kg=None,
                delta_30d_kg=None,
                min_kg=None,
                max_kg=None,
            )
        # entries are ordered newest-first (repository default)
        latest = entries[0]
        weights = [e.weight_kg for e in entries]

        def _delta_since(days_back: int) -> Decimal | None:
            cutoff = end - timedelta(days=days_back)
            if latest.measured_at <= cutoff:
                return None
            prior = [e for e in entries if e.measured_at <= cutoff]
            if not prior:
                return None
            return latest.weight_kg - prior[0].weight_kg

        return WeightTrendResponse(
            entries=entries,
            latest_kg=latest.weight_kg,
            delta_7d_kg=_delta_since(7),
            delta_30d_kg=_delta_since(30),
            min_kg=min(weights),
            max_kg=max(weights),
        )
