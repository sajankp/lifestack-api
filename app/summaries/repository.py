from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.summaries.models import WeeklySummary


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
        q = select(WeeklySummary).where(WeeklySummary.workspace_id == workspace_id)
        cq = (
            select(func.count())
            .select_from(WeeklySummary)
            .where(WeeklySummary.workspace_id == workspace_id)
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
                .where(WeeklySummary.workspace_id == workspace_id)
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
