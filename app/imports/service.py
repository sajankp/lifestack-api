import asyncio
import csv
import hashlib
import io
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.audit import AuditLogger
from app.core.exceptions import NotFoundError, ValidationError
from app.finance.models import Account, Currency
from app.imports.models import (
    ImportBatch,
    ImportError,
    ImportModule,
    ImportPreviewRow,
    ImportStatus,
)
from app.imports.repository import ImportRepository
from app.imports.schemas import SPENDEE_TRANSACTION_HEADERS, TEMPLATE_HEADERS
from app.investing.models import Holding
from app.spending.models import (
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
    TransactionSourceType,
    TransactionType,
)

try:
    import boto3  # type: ignore
except Exception:  # pragma: no cover
    boto3 = None


class ImportService:
    MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024
    MAX_VALIDATION_ROWS = 10_000
    COMMIT_CHUNK_SIZE = 1000

    def __init__(self, repository: ImportRepository, session: AsyncSession):
        self.repository = repository
        self.session = session

    def template_csv(self, module: ImportModule) -> str:
        header = TEMPLATE_HEADERS[module]
        lines = [",".join(header)]
        if module == ImportModule.spending_transactions:
            lines.append("2026-05-01T09:30:00Z,expense,42.50,Food & Dining,Breakfast")
        elif module == ImportModule.spending_budgets:
            lines.append("2026-05-01,Food & Dining,800.00")
        else:
            lines.append("AAPL,brokerage,10,150.25,USD")
        return "\n".join(lines) + "\n"

    async def _hash_file(self, upload: UploadFile) -> tuple[str, int]:
        hasher = hashlib.sha256()
        total = 0
        await upload.seek(0)
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > self.MAX_FILE_SIZE_BYTES:
                raise ValidationError(
                    detail=f"File exceeds the maximum allowed limit of {self.MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB."
                )
            hasher.update(chunk)
        await upload.seek(0)
        return hasher.hexdigest(), total

    async def _store_file_if_configured(self, batch: ImportBatch, upload: UploadFile) -> None:
        backend = settings.IMPORT_STORAGE_BACKEND.lower()
        batch.storage_backend = backend
        if backend == "none":
            return

        await upload.seek(0)
        key = f"imports/{batch.workspace_id}/{batch.public_id}/source.csv"
        if backend == "local":
            base = Path(settings.IMPORT_LOCAL_PATH)
            path = base / key
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as f:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            batch.storage_key = str(path)
            await upload.seek(0)
            return

        if backend == "s3":
            endpoint = settings.IMPORT_S3_ENDPOINT
            bucket = settings.IMPORT_S3_BUCKET
            region = settings.IMPORT_S3_REGION
            access_key = settings.IMPORT_S3_ACCESS_KEY
            secret_key = settings.IMPORT_S3_SECRET_KEY
            force_path_style = settings.IMPORT_S3_FORCE_PATH_STYLE
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
            await asyncio.to_thread(client.upload_fileobj, upload.file, bucket, key)
            batch.storage_key = f"s3://{bucket}/{key}"
            await upload.seek(0)
            return

        raise ValidationError(detail=f"Unsupported IMPORT_STORAGE_BACKEND: {backend}")

    async def _category_maps(self, workspace_id: int) -> tuple[dict[str, int], dict[str, int]]:
        rows = (
            (
                await self.session.execute(
                    select(SpendingCategory).where(SpendingCategory.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        by_name = {c.normalized_name: c.id for c in rows if c.id is not None}
        by_public = {str(c.public_id): c.id for c in rows if c.id is not None}
        return by_name, by_public

    async def _account_map(self, workspace_id: int) -> dict[str, int]:
        rows = (
            (
                await self.session.execute(
                    select(Account).where(Account.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        return {a.name.strip().lower(): a.id for a in rows if a.id is not None}

    async def _currency_set(self) -> set[str]:
        rows = (await self.session.execute(select(Currency.code))).scalars().all()
        return {r.upper() for r in rows}

    @staticmethod
    def _norm(value: str | None) -> str:
        return (value or "").strip()

    @staticmethod
    def _enum_value(value: object) -> str:
        return value.value if hasattr(value, "value") else str(value)

    async def validate_upload(
        self,
        workspace_id: int,
        user_id: int,
        module: ImportModule,
        upload: UploadFile,
        audit_logger: AuditLogger,
    ) -> tuple[ImportBatch, list[ImportError]]:
        if not upload.filename:
            raise ValidationError(detail="filename is required")
        if not upload.filename.lower().endswith(".csv"):
            raise ValidationError(detail="Only .csv files are supported in stage 1")

        file_hash, file_size = await self._hash_file(upload)
        batch = ImportBatch(
            workspace_id=workspace_id,
            user_id=user_id,
            module=module,
            status=ImportStatus.uploaded,
            filename=Path(upload.filename).name,
            content_type=upload.content_type,
            file_size_bytes=file_size,
            file_sha256=file_hash,
        )
        batch = await self.repository.create_batch(batch)
        await self._store_file_if_configured(batch, upload)

        by_name, by_public = await self._category_maps(workspace_id)
        account_map = await self._account_map(workspace_id)
        currency_set = await self._currency_set()

        await upload.seek(0)
        wrapped = io.TextIOWrapper(upload.file, encoding="utf-8-sig", newline="")
        try:
            reader = csv.DictReader(wrapped)
            headers = reader.fieldnames or []
            header_mode = "default"
            valid_headers = [TEMPLATE_HEADERS[module]]
            if module == ImportModule.spending_transactions:
                valid_headers.append(SPENDEE_TRANSACTION_HEADERS)
            if headers not in valid_headers:
                err = ImportError(
                    import_batch_id=batch.id,
                    row_number=1,
                    field_name="header",
                    error_code="invalid_header",
                    message=(
                        "Unexpected CSV headers. Expected one of: "
                        + " OR ".join([str(h) for h in valid_headers])
                    ),
                    raw_value=",".join(headers),
                )
                await self.repository.add_errors([err])
                batch.status = ImportStatus.failed_validation
                batch.validated_at = datetime.now(UTC)
                batch.error_rows = 1
                batch = await self.repository.save_batch(batch)
                return batch, [err]
            if (
                module == ImportModule.spending_transactions
                and headers == SPENDEE_TRANSACTION_HEADERS
            ):
                header_mode = "spendee"

            errors: list[ImportError] = []
            previews: list[ImportPreviewRow] = []
            total_rows = 0
            valid_rows = 0

            for row_no, row in enumerate(reader, start=2):
                if total_rows >= self.MAX_VALIDATION_ROWS:
                    raise ValidationError(
                        detail=f"File exceeds the maximum allowed limit of {self.MAX_VALIDATION_ROWS} rows."
                    )
                total_rows += 1
                row_errors: list[ImportError] = []

                def add_error(
                    field: str,
                    code: str,
                    msg: str,
                    value: str | None = None,
                    *,
                    current_row_no: int = row_no,
                    current_row_errors: list[ImportError] = row_errors,
                ) -> None:
                    current_row_errors.append(
                        ImportError(
                            import_batch_id=batch.id,
                            row_number=current_row_no,
                            field_name=field,
                            error_code=code,
                            message=msg,
                            raw_value=value,
                        )
                    )

                payload: dict
                if module == ImportModule.spending_transactions:
                    if header_mode == "spendee":
                        occurred_raw = self._norm(row.get("Date"))
                        raw_type = self._norm(row.get("Type"))
                        type_raw = raw_type.lower() if raw_type else ""
                        amount_raw = self._norm(row.get("Amount"))
                        category_raw = self._norm(row.get("Category name"))
                        description_raw = self._norm(row.get("Note")) or None
                        wallet_name_raw = self._norm(row.get("Wallet")) or None
                        labels_raw = self._norm(row.get("Labels")) or None
                    else:
                        occurred_raw = self._norm(row.get("occurred_at"))
                        raw_type = self._norm(row.get("type"))
                        type_raw = raw_type.lower() if raw_type else ""
                        amount_raw = self._norm(row.get("amount"))
                        category_raw = self._norm(row.get("category"))
                        description_raw = self._norm(row.get("description")) or None
                        wallet_name_raw = None
                        labels_raw = None

                    try:
                        occurred_at = datetime.fromisoformat(occurred_raw.replace("Z", "+00:00"))
                    except Exception:
                        add_error(
                            "occurred_at",
                            "invalid_datetime",
                            "occurred_at must be ISO datetime/date",
                            occurred_raw,
                        )
                        occurred_at = None

                    if type_raw not in {"income", "expense"}:
                        add_error(
                            "type", "invalid_enum", "type must be income or expense", type_raw
                        )

                    try:
                        amount = Decimal(amount_raw)
                        if header_mode == "spendee" and type_raw == "expense" and amount < 0:
                            amount = abs(amount)
                        if header_mode == "spendee" and type_raw == "income" and amount < 0:
                            add_error(
                                "amount",
                                "invalid_decimal",
                                "income rows cannot have negative amount",
                                amount_raw,
                            )
                            amount = None
                        if amount <= 0:
                            raise InvalidOperation
                    except Exception:
                        add_error(
                            "amount",
                            "invalid_decimal",
                            "amount must be a positive decimal",
                            amount_raw,
                        )
                        amount = None

                    category_id = None
                    if category_raw:
                        category_id = by_public.get(category_raw) or by_name.get(
                            category_raw.lower()
                        )
                    else:
                        add_error("category", "required", "category is required", category_raw)

                    payload = {
                        "occurred_at": occurred_at.isoformat() if occurred_at else None,
                        "type": type_raw,
                        "amount": str(amount) if amount is not None else None,
                        "category_id": category_id,
                        "category_name": category_raw if category_raw else None,
                        "description": description_raw,
                        "wallet_name": wallet_name_raw,
                        "labels": labels_raw,
                    }
                elif module == ImportModule.spending_budgets:
                    month_raw = self._norm(row.get("month_start"))
                    category_raw = self._norm(row.get("category"))
                    amount_raw = self._norm(row.get("amount"))

                    try:
                        month_start = datetime.fromisoformat(month_raw).date()
                        if month_start.day != 1:
                            raise ValueError
                    except Exception:
                        add_error(
                            "month_start",
                            "invalid_month",
                            "month_start must be YYYY-MM-01",
                            month_raw,
                        )
                        month_start = None

                    category_id = by_public.get(category_raw) or by_name.get(category_raw.lower())
                    if category_id is None:
                        add_error(
                            "category", "not_found", "category not found in workspace", category_raw
                        )

                    try:
                        amount = Decimal(amount_raw)
                        if amount <= 0:
                            raise InvalidOperation
                    except Exception:
                        add_error(
                            "amount",
                            "invalid_decimal",
                            "amount must be a positive decimal",
                            amount_raw,
                        )
                        amount = None

                    payload = {
                        "month_start": month_start.isoformat() if month_start else None,
                        "category_id": category_id,
                        "amount": str(amount) if amount is not None else None,
                    }
                else:
                    symbol_raw = self._norm(row.get("symbol")).upper()
                    account_name_raw = self._norm(row.get("account_name"))
                    quantity_raw = self._norm(row.get("quantity"))
                    avg_cost_raw = self._norm(row.get("avg_cost"))
                    currency_raw = self._norm(row.get("currency")).upper()

                    if not symbol_raw:
                        add_error("symbol", "required", "symbol is required", symbol_raw)
                    if account_name_raw.lower() not in account_map:
                        add_error(
                            "account_name",
                            "not_found",
                            "account_name not found in workspace",
                            account_name_raw,
                        )
                    if currency_raw not in currency_set:
                        add_error(
                            "currency",
                            "invalid_currency",
                            "currency must exist in reference table",
                            currency_raw,
                        )

                    try:
                        quantity = Decimal(quantity_raw)
                        if quantity <= 0:
                            raise InvalidOperation
                    except Exception:
                        add_error(
                            "quantity",
                            "invalid_decimal",
                            "quantity must be a positive decimal",
                            quantity_raw,
                        )
                        quantity = None

                    try:
                        avg_cost = Decimal(avg_cost_raw)
                        if avg_cost <= 0:
                            raise InvalidOperation
                    except Exception:
                        add_error(
                            "avg_cost",
                            "invalid_decimal",
                            "avg_cost must be a positive decimal",
                            avg_cost_raw,
                        )
                        avg_cost = None

                    payload = {
                        "symbol": symbol_raw,
                        "account_name": account_name_raw,
                        "quantity": str(quantity) if quantity is not None else None,
                        "avg_cost": str(avg_cost) if avg_cost is not None else None,
                        "currency": currency_raw,
                    }

                if row_errors:
                    errors.extend(row_errors)
                else:
                    valid_rows += 1
                    previews.append(
                        ImportPreviewRow(
                            import_batch_id=batch.id,
                            row_number=row_no,
                            payload_json=payload,
                        )
                    )

                if total_rows % 1000 == 0:
                    await self.repository.add_preview_rows(previews)
                    previews = []
                    if errors:
                        await self.repository.add_errors(errors)
                        errors = []

            if previews:
                await self.repository.add_preview_rows(previews)
            if errors:
                await self.repository.add_errors(errors)
        finally:
            wrapped.detach()

        persisted_errors = await self.repository.list_errors(batch.id, limit=10000)
        batch.total_rows = total_rows
        batch.valid_rows = valid_rows
        batch.error_rows = len(persisted_errors)
        batch.validated_at = datetime.now(UTC)
        batch.status = (
            ImportStatus.validated if batch.error_rows == 0 else ImportStatus.failed_validation
        )
        if batch.status == ImportStatus.failed_validation:
            await self.repository.clear_preview_rows(batch.id)
        batch.updated_at = datetime.now(UTC)
        batch = await self.repository.save_batch(batch)

        await audit_logger.log(
            workspace_id=workspace_id,
            actor_id=user_id,
            action="import_validated"
            if batch.status == ImportStatus.validated
            else "import_failed_validation",
            module="import",
            entity_type="import_batch",
            entity_id=batch.id,  # type: ignore[arg-type]
            details={
                "entity_public_id": str(batch.public_id),
                "before": None,
                "after": {
                    "module": self._enum_value(batch.module),
                    "status": self._enum_value(batch.status),
                    "total_rows": batch.total_rows,
                    "valid_rows": batch.valid_rows,
                    "error_rows": batch.error_rows,
                },
                "changed_fields": ["module", "status", "total_rows", "valid_rows", "error_rows"],
            },
        )

        return batch, list(persisted_errors)[:200]

    async def commit_import(
        self,
        workspace_id: int,
        user_id: int,
        import_public_id: uuid.UUID,
        audit_logger: AuditLogger,
    ) -> tuple[ImportBatch, int, list[str]]:
        batch = await self.repository.get_by_public_id(workspace_id, import_public_id)
        if not batch:
            raise NotFoundError(detail=f"Import batch with id {import_public_id} not found")
        if batch.status != ImportStatus.validated or batch.error_rows > 0:
            raise ValidationError(detail="Only fully validated imports can be committed")

        if not await self.repository.preview_rows_exist(batch.id):
            raise ValidationError(detail="No validated rows to commit")

        transitioned = await self.repository.begin_commit_transition(batch.id)
        if not transitioned:
            raise ValidationError(detail="Import is no longer in a committable state")
        batch.status = ImportStatus.committing
        batch.updated_at = datetime.now(UTC)
        await self.repository.save_batch(batch)

        inserted = 0
        auto_created_categories: list[str] = []
        try:
            category_name_to_id = await self._category_maps(workspace_id)
            by_name, _by_public = category_name_to_id
            offset = 0
            while True:
                rows = await self.repository.iter_preview_rows_chunk(
                    batch.id, self.COMMIT_CHUNK_SIZE, offset
                )
                if not rows:
                    break

                if batch.module == ImportModule.spending_transactions:
                    for row in rows:
                        p = row.payload_json
                        category_id = p.get("category_id")
                        if category_id is None:
                            category_name_raw = self._norm(p.get("category_name"))
                            if not category_name_raw:
                                raise ValidationError(detail="category is required")
                            category_name = category_name_raw.lower()
                            if category_name in by_name:
                                category_id = by_name[category_name]
                            else:
                                category = SpendingCategory(
                                    workspace_id=workspace_id,
                                    name=category_name_raw,
                                    normalized_name=category_name,
                                    is_system=False,
                                )
                                self.session.add(category)
                                await self.session.flush()
                                category_id = category.id
                                if category_id is None:
                                    raise ValidationError(detail="failed to create category")
                                by_name[category_name] = category_id
                                auto_created_categories.append(category.name)
                        tx = SpendingTransaction(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            category_id=int(category_id),
                            amount=Decimal(p["amount"]),
                            type=TransactionType(p["type"]),
                            occurred_at=datetime.fromisoformat(p["occurred_at"]),
                            description=p.get("description"),
                            wallet_name=p.get("wallet_name"),
                            labels=p.get("labels"),
                            source_type=TransactionSourceType.imported,
                            source_import_id=batch.id,
                            source_ref=f"{batch.public_id}:{row.row_number}",
                        )
                        self.session.add(tx)
                        inserted += 1
                elif batch.module == ImportModule.spending_budgets:
                    for row in rows:
                        p = row.payload_json
                        budget = SpendingBudget(
                            workspace_id=workspace_id,
                            category_id=int(p["category_id"]),
                            amount=Decimal(p["amount"]),
                            month_start=datetime.fromisoformat(p["month_start"]).date(),
                        )
                        self.session.add(budget)
                        inserted += 1
                else:
                    for row in rows:
                        p = row.payload_json
                        holding = Holding(
                            workspace_id=workspace_id,
                            user_id=user_id,
                            symbol=p["symbol"],
                            account_name=p["account_name"],
                            quantity=Decimal(p["quantity"]),
                            avg_cost=Decimal(p["avg_cost"]),
                            currency=p["currency"],
                        )
                        self.session.add(holding)
                        inserted += 1

                await self.session.flush()
                offset += self.COMMIT_CHUNK_SIZE
            batch.status = ImportStatus.completed
            batch.committed_at = datetime.now(UTC)
            batch.updated_at = datetime.now(UTC)
            await self.repository.save_batch(batch)
            await self.repository.clear_preview_rows(batch.id)

            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="import_committed",
                module="import",
                entity_type="import_batch",
                entity_id=batch.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(batch.public_id),
                    "before": {"status": self._enum_value(ImportStatus.validated)},
                    "after": {"status": self._enum_value(batch.status), "inserted_rows": inserted},
                    "changed_fields": ["status", "inserted_rows"],
                },
            )
        except Exception:
            await self.session.rollback()
            batch = await self.repository.get_by_public_id(workspace_id, import_public_id)
            if not batch:
                raise
            batch.status = ImportStatus.failed_commit
            batch.updated_at = datetime.now(UTC)
            await self.repository.save_batch(batch)
            raise

        return batch, inserted, auto_created_categories

    async def list_batches(self, workspace_id: int, limit: int, offset: int):
        return await self.repository.list_batches(workspace_id, limit, offset)

    async def get_batch_with_errors(self, workspace_id: int, public_id: uuid.UUID):
        batch = await self.repository.get_by_public_id(workspace_id, public_id)
        if not batch:
            raise NotFoundError(detail=f"Import batch with id {public_id} not found")
        errors = await self.repository.list_errors(batch.id, limit=200)
        return batch, errors

    async def delete_batch(
        self,
        workspace_id: int,
        user_id: int,
        public_id: uuid.UUID,
        audit_logger: AuditLogger,
    ) -> None:
        """Delete an import batch and its associated data.

        Completed spending-transaction imports are rolled back before deletion.
        Other completed modules are blocked until their committed rows have source metadata.
        """
        batch = await self.repository.get_by_public_id(workspace_id, public_id)
        if not batch:
            raise NotFoundError(detail=f"Import batch with id {public_id} not found")
        if batch.id is None:
            raise ValidationError(detail="Import batch is not persisted and cannot be deleted")

        if batch.status == ImportStatus.committing:
            raise ValidationError(
                detail=(
                    f"Import batch with status '{batch.status}' cannot be deleted. "
                    "Wait for the import commit to finish before deleting it."
                )
            )
        if batch.status == ImportStatus.completed:
            if batch.module != ImportModule.spending_transactions:
                raise ValidationError(
                    detail=(
                        f"Completed imports for module '{batch.module}' cannot be rolled back yet."
                    )
                )
            deleted_records = await self.repository.delete_spending_transactions_for_batch(
                workspace_id, batch.id
            )
            action = "import_rolled_back"
        else:
            deleted_records = 0
            action = "import_deleted"

        await audit_logger.log(
            workspace_id=workspace_id,
            actor_id=user_id,
            action=action,
            module="import",
            entity_type="import_batch",
            entity_id=batch.id,
            details={
                "entity_public_id": str(batch.public_id),
                "before": {
                    "module": self._enum_value(batch.module),
                    "status": self._enum_value(batch.status),
                    "total_rows": batch.total_rows,
                    "valid_rows": batch.valid_rows,
                    "error_rows": batch.error_rows,
                },
                "after": None,
                "changed_fields": ["status", "deleted_records"],
                "deleted_records": deleted_records,
            },
        )

        await self.repository.delete_batch(batch)
