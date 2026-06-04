import csv
import io
import json
import uuid
from asyncio import to_thread
from collections.abc import Iterable
from datetime import UTC, datetime
from zipfile import ZIP_DEFLATED, ZipFile

import structlog
from sqlalchemy import func, select

from app.core.audit import AuditLogger
from app.core.exceptions import APIError, ConflictError, NotFoundError, ValidationError
from app.exports.models import ExportFormat, ExportRecord, ExportStatus
from app.exports.repository import ExportRepository
from app.exports.schemas import SUPPORTED_MODULES, ExportCreate
from app.investing.models import CashBalance, Holding
from app.spending.models import SpendingBudget, SpendingCategory, SpendingTransaction
from app.todo.models import Todo

SCHEMA_VERSION = 1
SYNC_LIMIT_PER_MODULE = 5000

logger = structlog.get_logger(__name__)


def _row_to_dict(model: object) -> dict:
    if not hasattr(model, "model_dump"):
        return {}
    return model.model_dump(mode="json")


def _normalize_modules(requested_modules: Iterable[str]) -> list[str]:
    modules = []
    for module in requested_modules:
        normalized = module.strip().lower()
        if normalized not in SUPPORTED_MODULES:
            raise ValidationError(detail=f"Unsupported export module '{module}'")
        modules.append(normalized)
    if not modules:
        raise ValidationError(detail="At least one export module must be selected")
    return sorted(set(modules))


class ExportService:
    def __init__(self, repository: ExportRepository):
        self.repository = repository
        self.session = repository.session

    async def _count_module_rows(self, workspace_id: int, module: str) -> int:
        if module == "todo":
            query = select(func.count(Todo.id)).where(Todo.workspace_id == workspace_id)
            result = await self.session.execute(query)
            return int(result.scalar() or 0)

        if module == "spending":
            counts_q = select(
                select(func.count(SpendingCategory.id))
                .where(SpendingCategory.workspace_id == workspace_id)
                .scalar_subquery()
                .label("category_count"),
                select(func.count(SpendingTransaction.id))
                .where(SpendingTransaction.workspace_id == workspace_id)
                .scalar_subquery()
                .label("tx_count"),
                select(func.count(SpendingBudget.id))
                .where(SpendingBudget.workspace_id == workspace_id)
                .scalar_subquery()
                .label("budget_count"),
            )
            counts_row = (await self.session.execute(counts_q)).one()
            category_count = int(counts_row.category_count or 0)
            tx_count = int(counts_row.tx_count or 0)
            budget_count = int(counts_row.budget_count or 0)
            return category_count + tx_count + budget_count

        # investing
        holding_count_q = select(func.count(Holding.id)).where(Holding.workspace_id == workspace_id)
        cash_count_q = select(func.count(CashBalance.id)).where(
            CashBalance.workspace_id == workspace_id
        )
        holding_count = int((await self.session.execute(holding_count_q)).scalar() or 0)
        cash_count = int((await self.session.execute(cash_count_q)).scalar() or 0)
        return holding_count + cash_count

    async def _load_module_payload(self, workspace_id: int, module: str) -> dict[str, list[dict]]:
        async def _stream_to_rows(query):
            rows: list[dict] = []
            stream = await self.session.stream_scalars(query)
            async for row in stream:
                rows.append(_row_to_dict(row))
            return rows

        if module == "todo":
            todo_rows = await _stream_to_rows(
                select(Todo)
                .where(Todo.workspace_id == workspace_id)
                .order_by(Todo.created_at.asc())
            )
            return {"todos": todo_rows}

        if module == "spending":
            categories = await _stream_to_rows(
                select(SpendingCategory)
                .where(SpendingCategory.workspace_id == workspace_id)
                .order_by(SpendingCategory.created_at.asc())
            )
            transactions = await _stream_to_rows(
                select(SpendingTransaction)
                .where(SpendingTransaction.workspace_id == workspace_id)
                .order_by(SpendingTransaction.occurred_at.asc())
            )
            budgets = await _stream_to_rows(
                select(SpendingBudget)
                .where(SpendingBudget.workspace_id == workspace_id)
                .order_by(SpendingBudget.month_start.asc())
            )
            return {
                "categories": categories,
                "transactions": transactions,
                "budgets": budgets,
            }

        holdings = await _stream_to_rows(
            select(Holding)
            .where(Holding.workspace_id == workspace_id)
            .order_by(Holding.created_at.asc())
        )
        cash_balances = await _stream_to_rows(
            select(CashBalance)
            .where(CashBalance.workspace_id == workspace_id)
            .order_by(CashBalance.created_at.asc())
        )
        return {
            "holdings": holdings,
            "cash_balances": cash_balances,
        }

    def _build_json_artifact(self, payload: dict) -> tuple[bytes, str, str]:
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return content, "application/json", "lifestack-export.json"

    def _build_csv_artifact(self, payload: dict) -> tuple[bytes, str, str]:
        buffer = io.BytesIO()
        with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as archive:
            for module_name, module_payload in payload["data"].items():
                for section_name, rows in module_payload.items():
                    csv_buffer = io.StringIO()
                    writer = csv.DictWriter(
                        csv_buffer, fieldnames=sorted(rows[0].keys()) if rows else []
                    )
                    if rows:
                        writer.writeheader()
                        for row in rows:
                            writer.writerow(row)
                    archive.writestr(f"{module_name}/{section_name}.csv", csv_buffer.getvalue())
        return buffer.getvalue(), "application/zip", "lifestack-export-csv.zip"

    async def create_export(
        self,
        workspace_id: int,
        requested_by: int,
        export_in: ExportCreate,
        audit_logger: AuditLogger,
    ) -> ExportRecord:
        modules = _normalize_modules(export_in.modules)
        pending = await self.repository.get_pending_for_workspace(workspace_id)
        if pending is not None:
            raise ConflictError(detail="A pending export already exists for this workspace")

        for module in modules:
            module_count = await self._count_module_rows(workspace_id, module)
            if module_count > SYNC_LIMIT_PER_MODULE:
                raise APIError(
                    detail=(
                        f"Module '{module}' exceeds synchronous export limit "
                        f"({module_count} rows > {SYNC_LIMIT_PER_MODULE})"
                    ),
                    type_str="payload-too-large",
                    title="Payload Too Large",
                    status_code=413,
                )

        export_record = ExportRecord(
            workspace_id=workspace_id,
            requested_by=requested_by,
            format=export_in.format,
            schema_version=SCHEMA_VERSION,
            scope={"modules": modules},
            status=ExportStatus.pending,
        )
        export_record = await self.repository.create(export_record)

        await audit_logger.log(
            workspace_id=workspace_id,
            actor_id=requested_by,
            action="export_request",
            module="export",
            entity_type="export",
            entity_id=export_record.id,  # type: ignore[arg-type]
            details={
                "entity_public_id": str(export_record.public_id),
                "before": None,
                "after": {
                    "status": export_record.status,
                    "format": export_record.format,
                    "scope": export_record.scope,
                },
                "changed_fields": ["status", "format", "scope"],
            },
        )

        payload = {
            "schema_version": SCHEMA_VERSION,
            "workspace_id": workspace_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "data": {},
        }
        before_status = export_record.status
        try:
            for module in modules:
                payload["data"][module] = await self._load_module_payload(workspace_id, module)

            if export_record.format == ExportFormat.json:
                blob, mime_type, filename = await to_thread(self._build_json_artifact, payload)
            else:
                blob, mime_type, filename = await to_thread(self._build_csv_artifact, payload)

            export_record.artifact_blob = blob
            export_record.artifact_mime_type = mime_type
            export_record.artifact_filename = filename
            export_record.status = ExportStatus.ready
            export_record.completed_at = datetime.now(UTC)
            export_record.storage_key = f"db://exports/{export_record.public_id}"
            export_record = await self.repository.save(export_record, refresh=False)

            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=requested_by,
                action="export_generated",
                module="export",
                entity_type="export",
                entity_id=export_record.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(export_record.public_id),
                    "before": {"status": before_status},
                    "after": {
                        "status": export_record.status,
                        "storage_key": export_record.storage_key,
                    },
                    "changed_fields": ["status", "storage_key"],
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "export_generation_failed",
                workspace_id=workspace_id,
                export_public_id=str(export_record.public_id),
                requested_by=requested_by,
            )
            export_record.status = ExportStatus.failed
            export_record.error_message = str(exc)
            export_record.completed_at = datetime.now(UTC)
            export_record = await self.repository.save(export_record, refresh=False)

        return export_record

    async def get_export(
        self, workspace_id: int, export_public_id: uuid.UUID, include_blob: bool = False
    ) -> ExportRecord:
        record = await self.repository.get_by_public_id(
            workspace_id, export_public_id, include_blob=include_blob
        )
        if record is None:
            raise NotFoundError(detail=f"Export with id {export_public_id} not found")
        return record

    async def delete_export(self, workspace_id: int, export_public_id: uuid.UUID) -> None:
        """Delete an export record.

        Exports that are still pending (being generated) cannot be deleted.
        """
        record = await self.repository.get_by_public_id(workspace_id, export_public_id)
        if record is None:
            raise NotFoundError(detail=f"Export with id {export_public_id} not found")
        if record.status == ExportStatus.pending:
            raise ConflictError(
                detail="Export is still being generated. Wait for it to complete before deleting."
            )
        await self.repository.delete(record)
