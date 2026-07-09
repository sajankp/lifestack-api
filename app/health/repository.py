from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select

from app.core.pagination import DEFAULT_LIMIT
from app.core.repository import BaseRepository
from app.health.models import Medication, MedicationEvent, WeightEntry


class MedicationRepository(BaseRepository[Medication]):
    async def get_all(
        self,
        workspace_id: int,
        is_active: bool | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[Sequence[Medication], int]:
        base = select(Medication).where(Medication.workspace_id == workspace_id)
        if is_active is not None:
            base = base.where(Medication.is_active == is_active)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        items_q = base.order_by(Medication.created_at.desc()).limit(limit).offset(offset)
        result = await self.session.execute(items_q)
        return result.scalars().all(), total

    async def get_active(self, workspace_id: int) -> Sequence[Medication]:
        result = await self.session.execute(
            select(Medication).where(
                Medication.workspace_id == workspace_id, Medication.is_active.is_(True)
            )
        )
        return result.scalars().all()

    async def get_active_with_reminders(self, workspace_id: int) -> Sequence[Medication]:
        result = await self.session.execute(
            select(Medication).where(
                Medication.workspace_id == workspace_id,
                Medication.is_active.is_(True),
                Medication.reminders_enabled.is_(True),
            )
        )
        return result.scalars().all()

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> Medication | None:
        result = await self.session.execute(
            select(Medication).where(
                Medication.workspace_id == workspace_id, Medication.public_id == public_id
            )
        )
        return result.scalar_one_or_none()

    async def get_event_count(self, medication_id: int) -> int:
        result = await self.session.execute(
            select(func.count(MedicationEvent.id)).where(
                MedicationEvent.medication_id == medication_id
            )
        )
        return int(result.scalar() or 0)

    async def get_event_counts(self, medication_ids: Sequence[int]) -> dict[int, int]:
        if not medication_ids:
            return {}
        result = await self.session.execute(
            select(MedicationEvent.medication_id, func.count(MedicationEvent.id))
            .where(MedicationEvent.medication_id.in_(medication_ids))
            .group_by(MedicationEvent.medication_id)
        )
        return dict(result.all())


class MedicationEventRepository(BaseRepository[MedicationEvent]):
    async def get_by_slot(
        self, medication_id: int, scheduled_for: datetime
    ) -> MedicationEvent | None:
        result = await self.session.execute(
            select(MedicationEvent).where(
                MedicationEvent.medication_id == medication_id,
                MedicationEvent.scheduled_for == scheduled_for,
            )
        )
        return result.scalar_one_or_none()

    async def get_for_medications_between(
        self, medication_ids: Sequence[int], start: datetime, end: datetime
    ) -> Sequence[MedicationEvent]:
        if not medication_ids:
            return []
        result = await self.session.execute(
            select(MedicationEvent).where(
                MedicationEvent.medication_id.in_(medication_ids),
                MedicationEvent.scheduled_for >= start,
                MedicationEvent.scheduled_for <= end,
            )
        )
        return result.scalars().all()

    async def get_status_counts_for_workspace(
        self, workspace_id: int, start: datetime, end: datetime
    ) -> dict[str, int]:
        result = await self.session.execute(
            select(MedicationEvent.status, func.count(MedicationEvent.id))
            .where(
                MedicationEvent.workspace_id == workspace_id,
                MedicationEvent.scheduled_for >= start,
                MedicationEvent.scheduled_for <= end,
            )
            .group_by(MedicationEvent.status)
        )
        return dict(result.all())


class WeightEntryRepository(BaseRepository[WeightEntry]):
    async def get_range(
        self,
        workspace_id: int,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[Sequence[WeightEntry], int]:
        base = select(WeightEntry).where(WeightEntry.workspace_id == workspace_id)
        if start is not None:
            base = base.where(WeightEntry.measured_at >= start)
        if end is not None:
            base = base.where(WeightEntry.measured_at <= end)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        items_q = base.order_by(WeightEntry.measured_at.desc()).limit(limit).offset(offset)
        result = await self.session.execute(items_q)
        return result.scalars().all(), total

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> WeightEntry | None:
        result = await self.session.execute(
            select(WeightEntry).where(
                WeightEntry.workspace_id == workspace_id, WeightEntry.public_id == public_id
            )
        )
        return result.scalar_one_or_none()
