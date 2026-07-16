from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.summaries.models import WeeklySummary, WorkspaceSummarySetting


class WeeklySummaryRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list(
        self,
        workspace_id: int,
        from_date: date | None,
        to_date: date | None,
        limit: int,
        offset: int,
    ):
        # spec-076: listing defaults to latest-non-superseded — a superseded
        # row is retained (never deleted) but is no longer the record of truth
        # for its week, so it drops out of the default list/latest views.
        q = select(WeeklySummary).where(
            WeeklySummary.workspace_id == workspace_id,
            WeeklySummary.superseded_by_id.is_(None),
        )
        cq = (
            select(func.count())
            .select_from(WeeklySummary)
            .where(
                WeeklySummary.workspace_id == workspace_id,
                WeeklySummary.superseded_by_id.is_(None),
            )
        )
        if from_date:
            q = q.where(WeeklySummary.week_start >= from_date)
            cq = cq.where(WeeklySummary.week_start >= from_date)
        if to_date:
            q = q.where(WeeklySummary.week_end <= to_date)
            cq = cq.where(WeeklySummary.week_end <= to_date)
        q = q.order_by(WeeklySummary.week_start.desc()).offset(offset).limit(limit)
        return list((await self.session.execute(q)).scalars().all()), int(
            (await self.session.execute(cq)).scalar_one()
        )

    async def latest(self, workspace_id: int):
        return (
            await self.session.execute(
                select(WeeklySummary)
                .where(
                    WeeklySummary.workspace_id == workspace_id,
                    WeeklySummary.superseded_by_id.is_(None),
                )
                .order_by(WeeklySummary.week_start.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def by_public_id(self, workspace_id: int, public_id):
        return (
            await self.session.execute(
                select(WeeklySummary).where(
                    WeeklySummary.workspace_id == workspace_id, WeeklySummary.public_id == public_id
                )
            )
        ).scalar_one_or_none()

    async def mark_read(self, summary: WeeklySummary) -> WeeklySummary:
        """Stamp read_at on first read; idempotent — a second read does not move
        the timestamp (spec-080)."""
        if summary.read_at is None:
            summary.read_at = datetime.now(UTC)
            await self.session.flush()
        return summary

    async def supersede(
        self, old: WeeklySummary, new: WeeklySummary, reason: str | None
    ) -> WeeklySummary:
        """spec-076 regeneration: the old row is retained forever (no cap on
        history) and pointed at its replacement; the new row carries the
        regeneration trail. Never deletes or overwrites the old row in place.

        old and new necessarily share (workspace_id, week_start), which is
        exactly the partial unique index (WHERE superseded_by_id IS NULL)
        that enforces "at most one current row per week" -- inserting new
        (superseded_by_id NULL) while old is still NULL would violate it. So
        old is first pointed at itself (a valid self-reference, satisfying
        the FK) purely to flip it out of the NULL/"current" set before new is
        inserted, then repointed at new's real id once it exists.
        """
        new.regenerated_at = datetime.now(UTC)
        new.regeneration_reason = reason

        old.superseded_by_id = old.id
        self.session.add(old)
        await self.session.flush()

        self.session.add(new)
        await self.session.flush()

        old.superseded_by_id = new.id
        self.session.add(old)
        await self.session.flush()
        await self.session.refresh(new)
        return new


class WorkspaceSummarySettingRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_workspace(self, workspace_id: int) -> WorkspaceSummarySetting | None:
        return (
            await self.session.execute(
                select(WorkspaceSummarySetting).where(
                    WorkspaceSummarySetting.workspace_id == workspace_id
                )
            )
        ).scalar_one_or_none()

    async def upsert(
        self, workspace_id: int, *, cadence_day_of_week: int, cadence_hour_utc: int
    ) -> WorkspaceSummarySetting:
        existing = await self.get_by_workspace(workspace_id)
        now = datetime.now(UTC)
        if existing:
            existing.cadence_day_of_week = cadence_day_of_week
            existing.cadence_hour_utc = cadence_hour_utc
            existing.updated_at = now
            self.session.add(existing)
            await self.session.flush()
            await self.session.refresh(existing)
            return existing

        row = WorkspaceSummarySetting(
            workspace_id=workspace_id,
            cadence_day_of_week=cadence_day_of_week,
            cadence_hour_utc=cadence_hour_utc,
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return row

    async def list_due(
        self, workspace_ids: list[int], day_of_week: int, hour_utc: int
    ) -> list[int]:
        """Workspaces among `workspace_ids` whose configured cadence matches
        the given (day_of_week, hour_utc) tick. Workspaces with no row use the
        spec-076-preserved default of Monday 01:30 UTC — callers pass the tick
        time truncated to the hour, so hour 1 catches that default."""
        configured = {
            row.workspace_id: row
            for row in (
                await self.session.execute(
                    select(WorkspaceSummarySetting).where(
                        WorkspaceSummarySetting.workspace_id.in_(workspace_ids)
                    )
                )
            ).scalars()
        }
        due = []
        for workspace_id in workspace_ids:
            setting = configured.get(workspace_id)
            ws_day = setting.cadence_day_of_week if setting else 0
            ws_hour = setting.cadence_hour_utc if setting else 1
            if ws_day == day_of_week and ws_hour == hour_utc:
                due.append(workspace_id)
        return due
