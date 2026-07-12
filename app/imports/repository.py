import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance.models import CapitalTransfer
from app.finance.statement_models import AccountStatement, StatementLine
from app.imports.models import ImportBatch, ImportError, ImportPreviewRow, ImportStatus
from app.investing.models import CashBalance, InvestingOrder
from app.spending.models import SpendingBudget, SpendingTransaction


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

    async def get_by_ids(self, workspace_id: int, ids: set[int]) -> dict[int, ImportBatch]:
        if not ids:
            return {}
        res = await self.session.execute(
            select(ImportBatch).where(
                ImportBatch.workspace_id == workspace_id,
                ImportBatch.id.in_(ids),
            )
        )
        return {batch.id: batch for batch in res.scalars().all() if batch.id is not None}

    async def list_batches(
        self, workspace_id: int, limit: int, offset: int
    ) -> tuple[Sequence[ImportBatch], int]:
        count_stmt = (
            select(func.count())
            .select_from(ImportBatch)
            .where(ImportBatch.workspace_id == workspace_id)
        )
        total = (await self.session.execute(count_stmt)).scalar_one()
        rows_stmt = (
            select(ImportBatch)
            .where(ImportBatch.workspace_id == workspace_id)
            .order_by(ImportBatch.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self.session.execute(rows_stmt)).scalars().all()
        return rows, total

    async def list_pending_review(
        self, workspace_id: int, limit: int = 5
    ) -> tuple[Sequence[ImportBatch], int]:
        """Import batches validated but not yet committed — the morning
        briefing's "pending review" line (spec-067)."""
        pending_statuses = (ImportStatus.uploaded, ImportStatus.validated)
        count_stmt = (
            select(func.count())
            .select_from(ImportBatch)
            .where(
                ImportBatch.workspace_id == workspace_id,
                ImportBatch.status.in_(pending_statuses),
            )
        )
        total = (await self.session.execute(count_stmt)).scalar_one()
        rows_stmt = (
            select(ImportBatch)
            .where(
                ImportBatch.workspace_id == workspace_id,
                ImportBatch.status.in_(pending_statuses),
            )
            .order_by(ImportBatch.created_at.asc())
            .limit(limit)
        )
        rows = (await self.session.execute(rows_stmt)).scalars().all()
        return rows, int(total)

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

    async def preview_rows_exist(self, import_batch_id: int) -> bool:
        stmt = (
            select(ImportPreviewRow.id)
            .where(ImportPreviewRow.import_batch_id == import_batch_id)
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def iter_preview_rows_chunk(
        self, import_batch_id: int, limit: int, offset: int
    ) -> Sequence[ImportPreviewRow]:
        stmt = (
            select(ImportPreviewRow)
            .where(ImportPreviewRow.import_batch_id == import_batch_id)
            .order_by(ImportPreviewRow.row_number.asc())
            .limit(limit)
            .offset(offset)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def clear_preview_rows(self, import_batch_id: int) -> None:
        await self.session.execute(
            delete(ImportPreviewRow).where(ImportPreviewRow.import_batch_id == import_batch_id)
        )

    async def delete_spending_transactions_for_batch(
        self, workspace_id: int, import_batch_id: int | None
    ) -> int:
        if import_batch_id is None:
            raise ValueError("import_batch_id is required for spending import rollback")
        result = await self.session.execute(
            delete(SpendingTransaction).where(
                SpendingTransaction.workspace_id == workspace_id,
                SpendingTransaction.source_import_id == import_batch_id,
            )
        )
        return result.rowcount or 0

    async def delete_spending_budgets_for_batch(
        self, workspace_id: int, import_batch_id: int | None
    ) -> int:
        if import_batch_id is None:
            raise ValueError("import_batch_id is required for budget import rollback")
        result = await self.session.execute(
            delete(SpendingBudget).where(
                SpendingBudget.workspace_id == workspace_id,
                SpendingBudget.source_import_id == import_batch_id,
            )
        )
        return result.rowcount or 0

    async def delete_capital_transfers_for_batch(
        self, workspace_id: int | None, import_batch_id: int | None
    ) -> int:
        if workspace_id is None or import_batch_id is None:
            raise ValueError(
                "workspace_id and import_batch_id are required for transfer import rollback"
            )
        result = await self.session.execute(
            delete(CapitalTransfer).where(
                CapitalTransfer.workspace_id == workspace_id,
                CapitalTransfer.source_import_id == import_batch_id,
            )
        )
        return result.rowcount or 0

    async def delete_account_statement_for_batch(
        self, workspace_id: int | None, import_batch_id: int | None
    ) -> int:
        """Delete the AccountStatement + its StatementLines created by this
        import batch. Statement lines are pure metadata (INV-1) — clearing
        them never touches spending_transactions or capital_transfers, only
        the match *reference* on the (deleted) line disappears."""
        if workspace_id is None or import_batch_id is None:
            raise ValueError(
                "workspace_id and import_batch_id are required for statement import rollback"
            )
        statement_id = (
            await self.session.execute(
                select(AccountStatement.id).where(
                    AccountStatement.workspace_id == workspace_id,
                    AccountStatement.import_batch_id == import_batch_id,
                )
            )
        ).scalar_one_or_none()
        if statement_id is None:
            return 0
        result = await self.session.execute(
            delete(StatementLine).where(StatementLine.statement_id == statement_id)
        )
        deleted = result.rowcount or 0
        await self.session.execute(
            delete(AccountStatement).where(AccountStatement.id == statement_id)
        )
        return deleted

    async def list_investing_orders_for_batch(
        self, workspace_id: int | None, import_batch_id: int | None
    ) -> Sequence[InvestingOrder]:
        if workspace_id is None or import_batch_id is None:
            raise ValueError(
                "workspace_id and import_batch_id are required for order import rollback"
            )
        result = await self.session.execute(
            select(InvestingOrder).where(
                InvestingOrder.workspace_id == workspace_id,
                InvestingOrder.source_import_id == import_batch_id,
            )
        )
        return result.scalars().all()

    async def delete_cash_balances_for_import(
        self, workspace_id: int | None, import_batch_id: int | None
    ) -> int:
        if workspace_id is None or import_batch_id is None:
            raise ValueError("workspace_id and import_batch_id are required")
        result = await self.session.execute(
            delete(CashBalance).where(
                CashBalance.workspace_id == workspace_id,
                CashBalance.source_import_id == import_batch_id,
            )
        )
        return result.rowcount or 0

    async def delete_cash_balances_by_trigger_refs(
        self, workspace_id: int | None, trigger_type: str, trigger_refs: Sequence[uuid.UUID]
    ) -> int:
        if workspace_id is None:
            raise ValueError("workspace_id is required to delete cash balances")
        if not trigger_refs:
            return 0
        result = await self.session.execute(
            delete(CashBalance).where(
                CashBalance.workspace_id == workspace_id,
                CashBalance.trigger_type == trigger_type,
                CashBalance.trigger_ref.in_(trigger_refs),
            )
        )
        return result.rowcount or 0

    async def delete_investing_orders_for_batch(
        self, workspace_id: int | None, import_batch_id: int | None
    ) -> int:
        if workspace_id is None or import_batch_id is None:
            raise ValueError(
                "workspace_id and import_batch_id are required for order import rollback"
            )
        result = await self.session.execute(
            delete(InvestingOrder).where(
                InvestingOrder.workspace_id == workspace_id,
                InvestingOrder.source_import_id == import_batch_id,
            )
        )
        return result.rowcount or 0

    async def begin_commit_transition(self, batch_id: int) -> bool:
        stmt = (
            update(ImportBatch)
            .where(
                ImportBatch.id == batch_id,
                ImportBatch.status == ImportStatus.validated,
                ImportBatch.error_rows == 0,
            )
            .values(status=ImportStatus.committing)
        )
        result = await self.session.execute(stmt)
        return (result.rowcount or 0) == 1

    async def delete_batch(self, batch: ImportBatch) -> None:
        """Delete all child rows then the batch itself."""
        batch_id = batch.id
        await self.session.execute(
            delete(ImportError).where(ImportError.import_batch_id == batch_id)
        )
        await self.session.execute(
            delete(ImportPreviewRow).where(ImportPreviewRow.import_batch_id == batch_id)
        )
        await self.session.delete(batch)
        await self.session.flush()
