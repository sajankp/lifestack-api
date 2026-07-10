import asyncio
import csv
import io
import json
import shutil
import tempfile
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import structlog
from sqlalchemy import func, select

from app.config import settings
from app.core.audit import AuditLogger
from app.core.exceptions import APIError, ConflictError, NotFoundError, ValidationError
from app.exports.models import ExportFormat, ExportRecord, ExportStatus
from app.exports.repository import ExportRepository
from app.exports.schemas import SUPPORTED_MODULES, ExportCreate
from app.finance.models import (
    Account,
    CapitalTransfer,
    WorkspaceCurrency,
    WorkspaceFinanceSetting,
)
from app.health.models import Medication, MedicationEvent, WeightEntry
from app.investing.models import (
    CashBalance,
    CorporateAction,
    Holding,
    InvestingOrder,
    OrderLot,
)
from app.spending.models import (
    CategoryGroup,
    RecurringTransaction,
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
)
from app.todo.models import RecurringTodoRule, Todo

try:
    import boto3  # type: ignore
except Exception:  # pragma: no cover
    boto3 = None

SCHEMA_VERSION = 2
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
        self._s3_client = None

    def _module_sections(self, workspace_id: int, module: str) -> list[tuple[str, object]]:
        """The ordered (entity_key, query) sections that make up one module.

        Single source of truth for both the JSON and CSV writers and the row
        counter, so a new entity is added in exactly one place (spec-070).
        Entity keys match ``schemas.EXPORT_MODULES``.
        """
        if module == "todo":
            return [
                (
                    "todos",
                    select(Todo)
                    .where(Todo.workspace_id == workspace_id)
                    .order_by(Todo.created_at.asc()),
                ),
                (
                    "recurring_rules",
                    select(RecurringTodoRule)
                    .where(RecurringTodoRule.workspace_id == workspace_id)
                    .order_by(RecurringTodoRule.created_at.asc()),
                ),
            ]
        if module == "spending":
            return [
                (
                    "category_groups",
                    select(CategoryGroup)
                    .where(CategoryGroup.workspace_id == workspace_id)
                    .order_by(CategoryGroup.created_at.asc()),
                ),
                (
                    "categories",
                    select(SpendingCategory)
                    .where(SpendingCategory.workspace_id == workspace_id)
                    .order_by(SpendingCategory.created_at.asc()),
                ),
                (
                    "transactions",
                    select(SpendingTransaction)
                    .where(SpendingTransaction.workspace_id == workspace_id)
                    .order_by(SpendingTransaction.occurred_at.asc()),
                ),
                (
                    "budgets",
                    select(SpendingBudget)
                    .where(SpendingBudget.workspace_id == workspace_id)
                    .order_by(SpendingBudget.start_month.asc()),
                ),
                (
                    "recurring_transactions",
                    select(RecurringTransaction)
                    .where(RecurringTransaction.workspace_id == workspace_id)
                    .order_by(RecurringTransaction.created_at.asc()),
                ),
            ]
        if module == "investing":
            return [
                (
                    "holdings",
                    select(Holding)
                    .where(Holding.workspace_id == workspace_id)
                    .order_by(Holding.created_at.asc()),
                ),
                (
                    "cash_balances",
                    select(CashBalance)
                    .where(CashBalance.workspace_id == workspace_id)
                    .order_by(CashBalance.created_at.asc()),
                ),
                (
                    "orders",
                    select(InvestingOrder)
                    .where(InvestingOrder.workspace_id == workspace_id)
                    .order_by(InvestingOrder.occurred_at.asc()),
                ),
                (
                    "order_lots",
                    select(OrderLot)
                    .where(OrderLot.workspace_id == workspace_id)
                    .order_by(OrderLot.acquired_at.asc()),
                ),
                (
                    "corporate_actions",
                    select(CorporateAction)
                    .where(CorporateAction.workspace_id == workspace_id)
                    .order_by(CorporateAction.ex_date.asc()),
                ),
            ]
        if module == "finance":
            return self._finance_sections(workspace_id)
        if module == "health":
            return [
                (
                    "medications",
                    select(Medication)
                    .where(Medication.workspace_id == workspace_id)
                    .order_by(Medication.created_at.asc()),
                ),
                (
                    "medication_events",
                    select(MedicationEvent)
                    .where(MedicationEvent.workspace_id == workspace_id)
                    .order_by(MedicationEvent.scheduled_for.asc()),
                ),
                (
                    "weight_entries",
                    select(WeightEntry)
                    .where(WeightEntry.workspace_id == workspace_id)
                    .order_by(WeightEntry.measured_at.asc()),
                ),
            ]
        raise ValueError(f"Unknown export module: {module}")

    def _accounts_section(self, workspace_id: int) -> tuple[str, object]:
        return (
            "accounts",
            select(Account)
            .where(Account.workspace_id == workspace_id)
            .order_by(Account.created_at.asc()),
        )

    def _finance_sections(self, workspace_id: int) -> list[tuple[str, object]]:
        return [
            self._accounts_section(workspace_id),
            (
                "capital_transfers",
                select(CapitalTransfer)
                .where(CapitalTransfer.workspace_id == workspace_id)
                .order_by(CapitalTransfer.occurred_at.asc()),
            ),
            (
                "finance_settings",
                select(WorkspaceFinanceSetting).where(
                    WorkspaceFinanceSetting.workspace_id == workspace_id
                ),
            ),
            (
                "workspace_currencies",
                select(WorkspaceCurrency)
                .where(WorkspaceCurrency.workspace_id == workspace_id)
                .order_by(WorkspaceCurrency.currency_code.asc()),
            ),
        ]

    def _resolve_module_blocks(
        self, workspace_id: int, modules: list[str]
    ) -> list[tuple[str, list[tuple[str, object]]]]:
        """Expand requested modules into ordered (module, sections) blocks.

        Auto-includes an accounts-only ``finance`` block when a module that
        references accounts (investing/spending) is selected without the finance
        module itself, so every exported account reference resolves (INV-3).
        """
        blocks = [(module, self._module_sections(workspace_id, module)) for module in modules]
        if "finance" not in modules and {"investing", "spending"} & set(modules):
            blocks.append(("finance", [self._accounts_section(workspace_id)]))
        return blocks

    async def _count_module_rows(self, workspace_id: int, module: str) -> int:
        total = 0
        for _key, query in self._module_sections(workspace_id, module):
            count_q = select(func.count()).select_from(query.order_by(None).subquery())
            total += int((await self.session.execute(count_q)).scalar() or 0)
        return total

    def _get_s3_client(self):
        if self._s3_client is not None:
            return self._s3_client, settings.EXPORT_S3_BUCKET

        endpoint = settings.EXPORT_S3_ENDPOINT
        bucket = settings.EXPORT_S3_BUCKET
        region = settings.EXPORT_S3_REGION
        access_key = settings.EXPORT_S3_ACCESS_KEY
        secret_key = settings.EXPORT_S3_SECRET_KEY
        force_path_style = settings.EXPORT_S3_FORCE_PATH_STYLE

        if not endpoint or not bucket or not access_key or not secret_key:
            raise ValidationError(
                detail="S3 storage backend configured without required credentials"
            )
        if boto3 is None:
            raise ValidationError(detail="S3 backend requires boto3")

        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=boto3.session.Config(
                s3={"addressing_style": "path" if force_path_style else "auto"}
            ),
        )
        self._s3_client = client
        return client, bucket

    async def _write_json_export(
        self, workspace_id: int, modules: list[str], filepath: str
    ) -> None:
        blocks = self._resolve_module_blocks(workspace_id, modules)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(
                f'{{"schema_version": {SCHEMA_VERSION}, "workspace_id": {workspace_id}, '
                f'"generated_at": "{datetime.now(UTC).isoformat()}", "data": {{'
            )
            for i, (module, sections) in enumerate(blocks):
                if i > 0:
                    f.write(",")
                f.write(f'"{module}": {{')
                for j, (key, query) in enumerate(sections):
                    if j > 0:
                        f.write(",")
                    await self._write_json_array(f, key, query)
                f.write("}")
            f.write("}}")

    async def _write_json_array(self, f, key: str, query) -> None:
        f.write(f'"{key}": [')
        stream = await self.session.stream_scalars(query)
        first = True
        async for row in stream:
            if not first:
                f.write(",")
            first = False
            f.write(json.dumps(_row_to_dict(row), ensure_ascii=False))
        f.write("]")

    async def _write_csv_export(self, workspace_id: int, modules: list[str], filepath: str) -> None:
        # ZipFile expects a local file path
        blocks = self._resolve_module_blocks(workspace_id, modules)
        with ZipFile(filepath, mode="w", compression=ZIP_DEFLATED) as archive:
            for module, sections in blocks:
                for key, query in sections:
                    await self._write_csv_section(archive, f"{module}/{key}.csv", query)

    async def _write_csv_section(self, archive: ZipFile, arcname: str, query) -> None:
        with (
            archive.open(arcname, mode="w") as inner_f,
            io.TextIOWrapper(inner_f, encoding="utf-8", newline="") as wrapper,
        ):
            writer = None
            stream = await self.session.stream_scalars(query)
            async for row in stream:
                row_dict = _row_to_dict(row)
                if writer is None:
                    writer = csv.DictWriter(wrapper, fieldnames=sorted(row_dict.keys()))
                    writer.writeheader()
                writer.writerow(row_dict)

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

        before_status = export_record.status
        temp_dir = tempfile.mkdtemp()
        temp_filepath = str(Path(temp_dir) / f"export_{export_record.public_id}")

        try:
            # 1. Generate the progressive export to local temp file
            if export_record.format == ExportFormat.json:
                await self._write_json_export(workspace_id, modules, temp_filepath)
                mime_type = "application/json"
                filename = "lifestack-export.json"
            else:
                await self._write_csv_export(workspace_id, modules, temp_filepath)
                mime_type = "application/zip"
                filename = "lifestack-export-csv.zip"

            # 2. Upload/transfer to configured storage backend
            backend = settings.EXPORT_STORAGE_BACKEND.lower()
            if backend == "db":
                export_record.artifact_blob = await asyncio.to_thread(
                    Path(temp_filepath).read_bytes
                )
                export_record.storage_key = f"db://exports/{export_record.public_id}"
            elif backend == "local":
                key = f"exports/{workspace_id}/{export_record.public_id}/{filename}"
                base = Path(settings.EXPORT_LOCAL_PATH)
                dest_path = base / key
                await asyncio.to_thread(dest_path.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.copy2, temp_filepath, dest_path)
                export_record.storage_key = f"local://{dest_path.absolute()}"
            elif backend == "s3":
                client, bucket = self._get_s3_client()
                key = f"exports/{workspace_id}/{export_record.public_id}/{filename}"
                with open(temp_filepath, "rb") as f:
                    await asyncio.to_thread(client.upload_fileobj, f, bucket, key)
                export_record.storage_key = f"s3://{bucket}/{key}"
            else:
                raise ValidationError(detail=f"Unsupported EXPORT_STORAGE_BACKEND: {backend}")

            export_record.artifact_mime_type = mime_type
            export_record.artifact_filename = filename
            export_record.status = ExportStatus.ready
            export_record.completed_at = datetime.now(UTC)
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
            raise APIError(
                detail=f"Export generation failed: {exc}",
                type_str="export-generation-failed",
                title="Export Generation Failed",
                status_code=500,
            ) from exc
        finally:
            await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)

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

    async def get_export_download(
        self, workspace_id: int, export_public_id: uuid.UUID
    ) -> tuple[str, str, str, any]:
        # Returns (backend, mime_type, filename, data_source)
        record = await self.repository.get_by_public_id(
            workspace_id, export_public_id, include_blob=True
        )
        if record is None:
            raise NotFoundError(detail=f"Export with id {export_public_id} not found")
        if record.status != ExportStatus.ready:
            raise NotFoundError(detail="Export artifact is not ready or has expired")

        storage_key = record.storage_key
        if not storage_key:
            raise NotFoundError(detail="Export artifact storage details are missing")

        mime_type = record.artifact_mime_type or "application/octet-stream"
        filename = record.artifact_filename or "export.bin"

        if storage_key.startswith("db://"):
            if record.artifact_blob is None:
                raise NotFoundError(detail="Export blob missing in database")
            return "db", mime_type, filename, record.artifact_blob

        elif storage_key.startswith("local://"):
            filepath = Path(storage_key[8:])
            if not await asyncio.to_thread(filepath.exists):
                raise NotFoundError(detail="Export file not found on local storage")
            return "local", mime_type, filename, str(filepath)

        elif storage_key.startswith("s3://"):
            parts = storage_key[5:].split("/", 1)
            if len(parts) != 2:
                raise ValidationError(detail="Invalid S3 storage key format")
            bucket, key = parts
            client, _ = self._get_s3_client()
            try:
                response = await asyncio.to_thread(client.get_object, Bucket=bucket, Key=key)
            except Exception as e:
                logger.error("failed_to_retrieve_s3_export", error=str(e), storage_key=storage_key)
                raise NotFoundError(detail="Export artifact not found in cloud storage") from e
            return "s3", mime_type, filename, response["Body"]

        else:
            # Try parsing as raw path if no scheme
            filepath = Path(storage_key)
            if filepath.is_absolute() and await asyncio.to_thread(filepath.exists):
                return "local", mime_type, filename, str(filepath)
            raise ValidationError(detail=f"Unsupported storage key scheme: {storage_key}")

    async def delete_export(self, workspace_id: int, export_public_id: uuid.UUID) -> None:
        """Delete an export record.

        Exports that are still pending (being generated) cannot be deleted.
        """
        record = await self.repository.get_by_public_id(
            workspace_id, export_public_id, include_blob=False
        )
        if record is None:
            raise NotFoundError(detail=f"Export with id {export_public_id} not found")
        if record.status == ExportStatus.pending:
            raise ConflictError(
                detail="Export is still being generated. Wait for it to complete before deleting."
            )

        storage_key = record.storage_key
        if storage_key:
            if storage_key.startswith("local://"):
                filepath = Path(storage_key[8:])
                try:
                    if await asyncio.to_thread(filepath.exists):
                        await asyncio.to_thread(filepath.unlink)
                except Exception as e:
                    logger.warning(
                        "failed_to_delete_local_file", error=str(e), storage_key=storage_key
                    )
            elif storage_key.startswith("s3://"):
                parts = storage_key[5:].split("/", 1)
                if len(parts) == 2:
                    bucket, key = parts
                    try:
                        client, _ = self._get_s3_client()
                        await asyncio.to_thread(client.delete_object, Bucket=bucket, Key=key)
                    except Exception as e:
                        logger.warning(
                            "failed_to_delete_s3_object", error=str(e), storage_key=storage_key
                        )
            elif not storage_key.startswith("db://"):
                # Try raw local path deletion
                filepath = Path(storage_key)
                try:
                    if filepath.is_absolute() and await asyncio.to_thread(filepath.exists):
                        await asyncio.to_thread(filepath.unlink)
                except Exception as e:
                    logger.warning(
                        "failed_to_delete_local_file", error=str(e), storage_key=storage_key
                    )

        await self.repository.delete(record)
