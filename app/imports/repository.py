import uuid
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.imports.models import ImportBatch, ImportError, ImportPreviewRow


class ImportRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_batch(self, batch: ImportBatch) -> ImportBatch:
        self.session.add(batch)
        await self.session.flush()
        await self.session.refresh(batch)
        return batch

    async def save_batch(self, batch: ImportBatch) -> ImportBatch:
        self.session.add(batch)
        await self.session.flush()
        await self.session.refresh(batch)
        return batch

    async def add_errors(self, rows: list[ImportError]) -> None:
        self.session.add_all(rows)
        await self.session.flush()

    async def add_preview_rows(self, rows: list[ImportPreviewRow]) -> None:
        self.session.add_all(rows)
        await self.session.flush()

    async def get_by_public_id(self, workspace_id: int, public_id: uuid.UUID) -> ImportBatch | None:
        res = await self.session.execute(
            select(ImportBatch).where(
                ImportBatch.workspace_id == workspace_id,
                ImportBatch.public_id == public_id,
            )
        )
        return res.scalar_one_or_none()

    async def list_batches(
        self, workspace_id: int, limit: int, offset: int
    ) -> tuple[Sequence[ImportBatch], int]:
        base = select(ImportBatch).where(ImportBatch.workspace_id == workspace_id)
        total = len((await self.session.execute(base)).scalars().all())
        rows = (
            (
                await self.session.execute(
                    base.order_by(ImportBatch.created_at.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return rows, total

    async def list_errors(self, import_batch_id: int, limit: int = 200) -> Sequence[ImportError]:
        res = await self.session.execute(
            select(ImportError)
            .where(ImportError.import_batch_id == import_batch_id)
            .order_by(ImportError.row_number.asc(), ImportError.id.asc())
            .limit(limit)
        )
        return res.scalars().all()

    async def iter_preview_rows(self, import_batch_id: int) -> Sequence[ImportPreviewRow]:
        res = await self.session.execute(
            select(ImportPreviewRow)
            .where(ImportPreviewRow.import_batch_id == import_batch_id)
            .order_by(ImportPreviewRow.row_number.asc())
        )
        return res.scalars().all()

    async def clear_preview_rows(self, import_batch_id: int) -> None:
        await self.session.execute(
            delete(ImportPreviewRow).where(ImportPreviewRow.import_batch_id == import_batch_id)
        )
