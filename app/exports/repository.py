import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exports.models import ExportRecord, ExportStatus


class ExportRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, record: ExportRecord) -> ExportRecord:
        self.session.add(record)
        await self.session.flush()
        await self.session.refresh(record)
        return record

    async def save(self, record: ExportRecord) -> ExportRecord:
        self.session.add(record)
        await self.session.flush()
        await self.session.refresh(record)
        return record

    async def get_by_public_id(
        self, workspace_id: int, public_id: uuid.UUID
    ) -> ExportRecord | None:
        result = await self.session.execute(
            select(ExportRecord).where(
                ExportRecord.workspace_id == workspace_id,
                ExportRecord.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_pending_for_workspace(self, workspace_id: int) -> ExportRecord | None:
        result = await self.session.execute(
            select(ExportRecord).where(
                ExportRecord.workspace_id == workspace_id,
                ExportRecord.status == ExportStatus.pending,
            )
        )
        return result.scalar_one_or_none()

    async def list_workspace_exports(
        self, workspace_id: int, limit: int = 50
    ) -> Sequence[ExportRecord]:
        result = await self.session.execute(
            select(ExportRecord)
            .where(ExportRecord.workspace_id == workspace_id)
            .order_by(ExportRecord.created_at.desc())
            .limit(limit)
        )
        return result.scalars().all()
