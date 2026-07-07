import asyncio
import contextlib
import csv
import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import openpyxl
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.audit import AuditLogger
from app.core.database import postgres
from app.core.exceptions import NotFoundError, ValidationError
from app.finance.models import (
    Account,
    Currency,
)
from app.finance.repository import AccountRepository, CurrencyRepository
from app.imports.cams_cas_import import validate_cams_cas_batch, validate_cams_cas_upload
from app.imports.demat_cas_import import (
    finalize_demat_cas_commit,
    validate_demat_cas_batch,
    validate_demat_cas_upload,
)
from app.imports.finance_transfers_import import (
    TEMPLATE_ROW as FINANCE_TRANSFERS_TEMPLATE_ROW,
)
from app.imports.finance_transfers_import import (
    commit_finance_transfers_chunk,
    validate_finance_transfer_row,
)
from app.imports.investing_constituents_import import (
    TEMPLATE_ROW as INVESTING_CONSTITUENTS_TEMPLATE_ROW,
)
from app.imports.investing_constituents_import import (
    check_weight_group_totals,
    commit_constituents_chunk,
    prepare_constituents_commit,
    validate_investing_constituent_row,
)
from app.imports.investing_orders_import import (
    TEMPLATE_ROWS as INVESTING_ORDERS_TEMPLATE_ROWS,
)
from app.imports.investing_orders_import import (
    commit_investing_orders_chunk,
    rollback_investing_orders_import,
    validate_investing_order_row,
)
from app.imports.models import (
    ImportBatch,
    ImportError,
    ImportModule,
    ImportPreviewRow,
    ImportStatus,
)
from app.imports.repository import ImportRepository
from app.imports.schemas import SPENDEE_TRANSACTION_HEADERS, TEMPLATE_HEADERS
from app.imports.spending_import import (
    SPENDING_BUDGETS_TEMPLATE_ROW,
    SPENDING_TRANSACTIONS_TEMPLATE_ROW,
    commit_spending_budgets_chunk,
    commit_spending_transactions_chunk,
    resolve_spending_transactions_fallback_account_id,
    validate_spending_budget_row,
    validate_spending_transaction_row,
    validate_spending_transactions_upload,
)
from app.investing.models import (
    Company,
    Instrument,
    InstrumentType,
)
from app.investing.order_service import InvestingOrderService
from app.investing.repository import (
    CashBalanceRepository,
    CompanyRepository,
    CorporateActionRepository,
    HoldingRepository,
    HoldingVerificationRepository,
    InstrumentRepository,
    InvestingOrderRepository,
    LotRepository,
)
from app.investing.service import InstrumentService
from app.spending.models import SpendingCategory

try:
    import boto3  # type: ignore
except Exception:  # pragma: no cover
    boto3 = None


class ImportService:
    MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024
    MAX_VALIDATION_ROWS = 10_000
    COMMIT_CHUNK_SIZE = 1000

    FUZZY_MAPPING = {
        "occurred_at": [
            "occurred_at",
            "occurred",
            "date",
            "time",
            "transaction_date",
            "datetime",
            "timestamp",
            "occured_at",
            "date_time",
        ],
        "type": ["type", "transaction_type", "kind", "txn_type"],
        "order_type": ["order_type", "trade_type", "type", "transaction_type", "kind"],
        "price_per_unit": [
            "price_per_unit",
            "price",
            "unit_price",
            "avg_price",
            "trade_price",
            "execution_price",
        ],
        "brokerage_fee": ["brokerage_fee", "commission", "broker_fee", "fee"],
        "tax_amount": ["tax_amount", "tax", "stt", "stamp_duty"],
        "other_fees": ["other_fees", "other", "misc_fees", "charges"],
        "exchange_name": ["exchange_name", "exchange", "market", "venue"],
        "amount": ["amount", "value", "sum", "price", "total", "cost", "val", "amt"],
        "category": ["category", "category_name", "group", "class", "tag", "cat"],
        "description": ["description", "note", "memo", "comment", "details", "desc"],
        "symbol": ["symbol", "ticker", "instrument_symbol", "sym"],
        "instrument_symbol": ["instrument_symbol", "symbol", "ticker", "sym"],
        "account_name": ["account_name", "account", "wallet", "brokerage", "portfolio", "acct"],
        "quantity": ["quantity", "qty", "shares", "units", "amount_of_shares", "vol", "volume"],
        "avg_cost": [
            "avg_cost",
            "average_cost",
            "cost",
            "avg_price",
            "buy_price",
            "average_price",
            "unit_price",
        ],
        "currency": ["currency", "ccy", "currency_code", "curr"],
        "instrument_type": ["instrument_type", "asset_type", "type", "inst_type"],
        "instrument_name": ["instrument_name", "fund_name", "scheme_name", "inst_name"],
        "company_name": ["company_name", "company", "issuer", "co_name"],
        "company_ticker": ["company_ticker", "ticker_symbol", "company_symbol", "co_ticker"],
        "weight": ["weight", "percentage", "pct", "allocation", "wt"],
        "as_of_date": ["as_of_date", "as_of", "date_as_of", "date", "asof"],
        "month_start": ["month_start", "month", "start_month", "date", "start_date"],
        "from_account": [
            "from_account",
            "from_account_name",
            "from",
            "source_account",
            "wallet",
            "account",
        ],
        "to_account": [
            "to_account",
            "to_account_name",
            "to",
            "destination_account",
            "target_account",
        ],
        "from_currency": ["from_currency", "from_currency_code", "currency"],
        "to_currency": ["to_currency", "to_currency_code"],
        "net_amount_received": ["net_amount_received", "net_amount", "amount"],
    }

    REQUIRED_HEADERS = {
        ImportModule.spending_transactions: {"occurred_at", "type", "amount", "category"},
        ImportModule.spending_budgets: {"month_start", "category", "amount"},
        ImportModule.investing_constituents: {
            "instrument_symbol",
            "company_name",
            "company_ticker",
            "weight",
            "as_of_date",
        },
        ImportModule.investing_orders: {
            "order_type",
            "symbol",
            "account_name",
            "quantity",
            "price_per_unit",
            "currency",
            "occurred_at",
        },
        ImportModule.finance_transfers: {
            "occurred_at",
            "from_account",
            "to_account",
            "from_currency",
            "to_currency",
            "gross_amount",
            "net_amount_received",
        },
    }

    def _smart_match_headers(self, file_headers: list[str], module: ImportModule) -> dict[str, str]:
        def normalize(s: str) -> str:
            return "".join(c for c in s.lower() if c.isalnum())

        expected_headers = TEMPLATE_HEADERS[module]
        mapping = {}
        used_file_headers = set()
        normalized_file_headers = {normalize(h): h for h in file_headers if h}

        for exp in expected_headers:
            exp_norm = normalize(exp)
            if exp_norm in normalized_file_headers:
                matching_header = normalized_file_headers[exp_norm]
                mapping[exp] = matching_header
                used_file_headers.add(matching_header)

        for exp in expected_headers:
            if exp in mapping:
                continue
            candidates = self.FUZZY_MAPPING.get(exp, [exp])
            norm_candidates = {normalize(c) for c in candidates}
            for fh in file_headers:
                if fh in used_file_headers:
                    continue
                if normalize(fh) in norm_candidates:
                    mapping[exp] = fh
                    used_file_headers.add(fh)
                    break
        return mapping

    def __init__(
        self,
        repository: ImportRepository,
        session: AsyncSession,
        order_service: InvestingOrderService | None = None,
    ):
        self.repository = repository
        self.session = session
        self.order_service = order_service
        self._cash_balance_cache: dict[tuple[int, str], Decimal] = {}
        self._cache_session: AsyncSession = session

    def _ensure_cache_session(self) -> None:
        """Clear the in-memory instance cache if the current session differs
        from the one it was built against. Entities/values cached against a
        stale session (e.g. a closed or replaced AsyncSession) must not leak
        into work done under a new session."""
        if self._cache_session is not self.session:
            self._cash_balance_cache = {}
            self._cache_session = self.session

    def template_csv(self, module: ImportModule) -> str:
        if module == ImportModule.investing_cams_cas:
            raise ValidationError(
                detail="CAMS CAS imports do not use a CSV template — upload the "
                "Consolidated Account Statement PDF directly"
            )
        if module == ImportModule.investing_demat_cas:
            raise ValidationError(
                detail="Demat CAS imports do not use a CSV template — upload the "
                "NSDL Consolidated Account Statement PDF directly"
            )
        header = TEMPLATE_HEADERS[module]
        lines = [",".join(header)]
        if module == ImportModule.spending_transactions:
            lines.append(SPENDING_TRANSACTIONS_TEMPLATE_ROW)
        elif module == ImportModule.spending_budgets:
            lines.append(SPENDING_BUDGETS_TEMPLATE_ROW)
        elif module == ImportModule.investing_constituents:
            lines.append(INVESTING_CONSTITUENTS_TEMPLATE_ROW)
        elif module == ImportModule.investing_orders:
            lines.extend(INVESTING_ORDERS_TEMPLATE_ROWS)
        elif module == ImportModule.finance_transfers:
            lines.append(FINANCE_TRANSFERS_TEMPLATE_ROW)
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
                    select(Account).where(
                        Account.workspace_id == workspace_id,
                        Account.is_active,
                    )
                )
            )
            .scalars()
            .all()
        )
        return {a.name.strip().lower(): a.id for a in rows if a.id is not None}

    async def _account_public_id_map(self, workspace_id: int) -> dict[str, uuid.UUID]:
        rows = (
            (
                await self.session.execute(
                    select(Account).where(
                        Account.workspace_id == workspace_id,
                        Account.is_active,
                    )
                )
            )
            .scalars()
            .all()
        )
        return {a.name.strip().lower(): a.public_id for a in rows}

    async def _currency_set(self) -> set[str]:
        rows = (await self.session.execute(select(Currency.code))).scalars().all()
        return {r.upper() for r in rows}

    async def _resolve_or_create_instrument(
        self, workspace_id: int, symbol: str, instrument_type: InstrumentType
    ) -> Instrument:
        instrument = (
            await self.session.execute(
                select(Instrument).where(
                    Instrument.workspace_id == workspace_id,
                    Instrument.symbol == symbol,
                )
            )
        ).scalar_one_or_none()
        if instrument is not None:
            if (
                instrument_type != InstrumentType.stock
                and instrument.instrument_type != instrument_type.value
            ):
                instrument.instrument_type = instrument_type.value
                instrument.updated_at = datetime.now(UTC)
                if instrument_type != InstrumentType.stock:
                    instrument.company_id = None
            return instrument

        company_id: int | None = None
        if instrument_type == InstrumentType.stock:
            company = (
                await self.session.execute(
                    select(Company).where(
                        Company.workspace_id == workspace_id,
                        Company.name == symbol,
                    )
                )
            ).scalar_one_or_none()
            if company is None:
                company = Company(workspace_id=workspace_id, name=symbol, ticker=symbol)
                self.session.add(company)
                await self.session.flush()
            company_id = company.id

        instrument = Instrument(
            workspace_id=workspace_id,
            symbol=symbol,
            name=symbol,
            instrument_type=instrument_type.value,
            company_id=company_id,
        )
        self.session.add(instrument)
        await self.session.flush()
        return instrument

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
        target_account_id: uuid.UUID | None = None,
    ) -> tuple[ImportBatch, str]:
        if module == ImportModule.investing_holdings:
            raise ValidationError(detail="investing-holdings imports are no longer supported")
        if not upload.filename:
            raise ValidationError(detail="filename is required")

        extra_json: dict | None = None
        if module == ImportModule.investing_cams_cas:
            extra_json = await validate_cams_cas_upload(
                self.session, workspace_id, upload.filename, target_account_id
            )
        elif module == ImportModule.investing_demat_cas:
            extra_json = await validate_demat_cas_upload(
                self.session, workspace_id, upload.filename, target_account_id
            )
        elif module == ImportModule.spending_transactions:
            extra_json = await validate_spending_transactions_upload(
                self.session, workspace_id, upload.filename, target_account_id
            )
        else:
            if target_account_id is not None:
                raise ValidationError(
                    detail="target_account_id is only supported for CAMS CAS and spending transaction imports"
                )
            if not (
                upload.filename.lower().endswith(".csv")
                or upload.filename.lower().endswith(".xlsx")
            ):
                raise ValidationError(detail="Only .csv and .xlsx files are supported")

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
            extra_json=extra_json,
        )
        batch = await self.repository.create_batch(batch)
        await self._store_file_if_configured(batch, upload)

        # Write to temp local directory for background worker
        temp_dir = Path("imports_temp")
        temp_dir.mkdir(exist_ok=True)
        ext = Path(upload.filename).suffix
        temp_filename = f"{uuid.uuid4()}{ext}"
        temp_path = temp_dir / temp_filename

        await upload.seek(0)
        with temp_path.open("wb") as f:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        await upload.seek(0)

        return batch, str(temp_path)

    async def validate_batch_file(
        self,
        workspace_id: int,
        user_id: int,
        batch: ImportBatch,
        file_path: str,
        audit_logger: AuditLogger,
        file_password: str | None = None,
    ) -> tuple[ImportBatch, list[ImportError]]:
        if batch.module == ImportModule.investing_cams_cas:
            return await validate_cams_cas_batch(
                self.session, self.repository, workspace_id, user_id, batch, file_path, audit_logger
            )
        if batch.module == ImportModule.investing_demat_cas:
            return await validate_demat_cas_batch(
                self.session,
                self.repository,
                workspace_id,
                user_id,
                batch,
                file_path,
                audit_logger,
                file_password,
            )

        by_name, by_public = await self._category_maps(workspace_id)
        account_map = await self._account_map(workspace_id)
        currency_set = await self._currency_set()

        # Fallback account for spending-transaction rows with no (matched)
        # account_name (spec-054): the import-level target account set at
        # upload time, else the workspace default. Resolved once, not per row.
        fallback_account_id: int | None = None
        if batch.module == ImportModule.spending_transactions:
            fallback_account_id = await resolve_spending_transactions_fallback_account_id(
                self.session, workspace_id, batch
            )

        instruments_map = {}
        order_account_pub_map: dict[str, uuid.UUID] = {}
        if batch.module in {ImportModule.investing_orders, ImportModule.finance_transfers}:
            order_account_pub_map = await self._account_public_id_map(workspace_id)
        if batch.module == ImportModule.investing_constituents:
            inst_rows = (
                (
                    await self.session.execute(
                        select(Instrument).where(Instrument.workspace_id == workspace_id)
                    )
                )
                .scalars()
                .all()
            )
            instruments_map = {inst.symbol.upper(): inst for inst in inst_rows}

        is_xlsx = file_path.lower().endswith(".xlsx")
        f = None
        wb = None

        if is_xlsx:
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            ws = wb.active
            rows_gen = ws.iter_rows(values_only=True)
            try:
                first_row = next(rows_gen)
                headers = [str(cell).strip() if cell is not None else "" for cell in first_row]
            except StopIteration:
                headers = []

            def xlsx_row_reader():
                for row_idx, r in enumerate(rows_gen, start=2):
                    if any(cell is not None for cell in r):
                        row_dict = {}
                        for c_idx, cell in enumerate(r):
                            if c_idx < len(headers):
                                row_dict[headers[c_idx]] = cell
                        yield row_idx, row_dict

            reader = xlsx_row_reader()
        else:
            f = open(file_path, encoding="utf-8-sig", newline="")  # noqa: SIM115
            csv_reader = csv.DictReader(f)
            headers = csv_reader.fieldnames or []

            def csv_row_reader():
                yield from enumerate(csv_reader, start=2)

            reader = csv_row_reader()

        try:
            mapping = self._smart_match_headers(headers, batch.module)
            header_mode = "default"
            if batch.module == ImportModule.spending_transactions and (
                headers[: len(SPENDEE_TRANSACTION_HEADERS)] == SPENDEE_TRANSACTION_HEADERS
            ):
                header_mode = "spendee"

            required = self.REQUIRED_HEADERS[batch.module]
            mapped_keys = set(mapping.keys())
            missing_required = required - mapped_keys

            if header_mode != "spendee" and missing_required:
                valid_headers = [TEMPLATE_HEADERS[batch.module]]
                if batch.module == ImportModule.spending_transactions:
                    valid_headers.append(SPENDEE_TRANSACTION_HEADERS)
                    valid_headers.append(SPENDEE_TRANSACTION_HEADERS + ["Author"])

                err = ImportError(
                    import_batch_id=batch.id,
                    row_number=1,
                    field_name="header",
                    error_code="invalid_header",
                    message=(
                        f"Unexpected headers. Missing required fields: {', '.join(missing_required)}. Expected format matching: "
                        + " OR ".join([str(h) for h in valid_headers])
                    ),
                    raw_value=",".join(headers),
                )
                await self.repository.add_errors([err])
                batch.status = ImportStatus.failed_validation
                batch.validated_at = datetime.now(UTC)
                batch.error_rows = 1
                batch.updated_at = datetime.now(UTC)
                batch = await self.repository.save_batch(batch)
                return batch, [err]

            errors: list[ImportError] = []
            previews: list[ImportPreviewRow] = []
            total_rows = 0
            valid_rows = 0
            weight_groups: dict[tuple[str, str], list[Decimal]] = {}

            for row_no, raw_row in reader:
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

                row = {}
                if header_mode == "spendee":
                    row = raw_row
                else:
                    for exp, actual in mapping.items():
                        cell_val = raw_row.get(actual)
                        row[exp] = str(cell_val).strip() if cell_val is not None else None

                payload: dict
                if batch.module == ImportModule.spending_transactions:
                    payload, _weight_entry = validate_spending_transaction_row(
                        row,
                        add_error,
                        header_mode=header_mode,
                        by_name=by_name,
                        by_public=by_public,
                        account_map=account_map,
                        fallback_account_id=fallback_account_id,
                    )
                elif batch.module == ImportModule.spending_budgets:
                    payload, _weight_entry = validate_spending_budget_row(
                        row, add_error, by_name=by_name, by_public=by_public
                    )
                elif batch.module == ImportModule.investing_orders:
                    payload, _weight_entry = validate_investing_order_row(
                        row,
                        add_error,
                        order_account_pub_map=order_account_pub_map,
                        currency_set=currency_set,
                    )
                elif batch.module == ImportModule.finance_transfers:
                    payload, _weight_entry = validate_finance_transfer_row(
                        row,
                        add_error,
                        order_account_pub_map=order_account_pub_map,
                        account_map=account_map,
                        currency_set=currency_set,
                    )
                else:
                    payload, weight_entry = validate_investing_constituent_row(
                        row, add_error, instruments_map
                    )
                    if weight_entry is not None:
                        key, weight = weight_entry
                        weight_groups.setdefault(key, []).append(weight)

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

            errors.extend(check_weight_group_totals(batch.id, weight_groups))

            if previews:
                await self.repository.add_preview_rows(previews)
            if errors:
                await self.repository.add_errors(errors)
        finally:
            if f is not None:
                f.close()
            if wb is not None:
                wb.close()

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

    async def start_commit(
        self,
        workspace_id: int,
        import_public_id: uuid.UUID,
    ) -> ImportBatch:
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
        return await self.repository.save_batch(batch)

    async def commit_batch(
        self,
        workspace_id: int,
        user_id: int,
        import_public_id: uuid.UUID,
        audit_logger: AuditLogger,
    ) -> tuple[ImportBatch, int, list[str]]:
        batch = await self.repository.get_by_public_id(workspace_id, import_public_id)
        if not batch:
            raise NotFoundError(detail=f"Import batch with id {import_public_id} not found")
        if batch.status != ImportStatus.committing:
            raise ValidationError(detail="Import is not in committing state")

        self._ensure_cache_session()

        inserted = 0
        auto_created_categories: list[str] = []
        demat_cas_report: list[dict] = []
        try:
            if batch.module == ImportModule.investing_constituents:
                company_cache = await prepare_constituents_commit(
                    self.session, self.repository, workspace_id, batch
                )

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
                    inserted += await commit_spending_transactions_chunk(
                        self.session,
                        workspace_id,
                        user_id,
                        batch,
                        rows,
                        by_name,
                        auto_created_categories,
                    )
                elif batch.module == ImportModule.spending_budgets:
                    inserted += await commit_spending_budgets_chunk(
                        self.session, workspace_id, batch, rows
                    )
                elif batch.module in {
                    ImportModule.investing_orders,
                    ImportModule.investing_cams_cas,
                }:
                    if self.order_service is None:
                        raise ValidationError(
                            detail="Order service is not available for this import type"
                        )
                    inserted += await commit_investing_orders_chunk(
                        self.order_service, workspace_id, user_id, batch, rows, audit_logger
                    )
                elif batch.module == ImportModule.finance_transfers:
                    inserted += await commit_finance_transfers_chunk(
                        self.session,
                        self.order_service,
                        workspace_id,
                        user_id,
                        batch,
                        rows,
                        self._cash_balance_cache,
                    )
                elif batch.module == ImportModule.investing_demat_cas:
                    # Accumulated across chunks; the HoldingVerification row is
                    # built once, after the loop, from the full report — this
                    # is a single verification snapshot, not N inserted rows.
                    for row in rows:
                        demat_cas_report.append(row.payload_json)
                else:  # ImportModule.investing_constituents
                    inserted += await commit_constituents_chunk(
                        self.session, workspace_id, rows, company_cache
                    )

                await self.session.flush()
                offset += self.COMMIT_CHUNK_SIZE

            if batch.module == ImportModule.investing_demat_cas:
                inserted = await finalize_demat_cas_commit(
                    self.session, workspace_id, batch, demat_cas_report
                )

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
        except Exception as e:
            await self.session.rollback()
            batch = await self.repository.get_by_public_id(workspace_id, import_public_id)
            if not batch:
                raise
            batch.status = ImportStatus.failed_commit
            batch.updated_at = datetime.now(UTC)
            if isinstance(e, ValidationError):
                batch.commit_error = e.detail
            elif isinstance(e, IntegrityError):
                batch.commit_error = f"Database conflict or integrity error: {e.orig}"
            else:
                batch.commit_error = str(e)
            await self.repository.save_batch(batch)
            await self.session.commit()
            if isinstance(e, IntegrityError):
                raise ValidationError(
                    detail=f"Database conflict or integrity error during import: {e.orig}"
                ) from e
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

    async def _rollback_investing_orders(
        self, workspace_id: int, user_id: int, import_batch_id: int
    ) -> int:
        """Roll back a committed investing-orders import.

        Thin delegator kept on the façade (rather than inlined at the call
        site) because it's exercised directly in unit tests
        (`test_import_order_rollback.py`). See
        `app/imports/investing_orders_import.py` for the implementation.
        """
        return await rollback_investing_orders_import(
            self.repository, self.order_service, workspace_id, user_id, import_batch_id
        )

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
            if batch.module == ImportModule.spending_transactions:
                deleted_records = await self.repository.delete_spending_transactions_for_batch(
                    workspace_id, batch.id
                )
            elif batch.module == ImportModule.spending_budgets:
                deleted_records = await self.repository.delete_spending_budgets_for_batch(
                    workspace_id, batch.id
                )
            elif batch.module in {
                ImportModule.investing_orders,
                ImportModule.investing_cams_cas,
            }:
                deleted_records = await self._rollback_investing_orders(
                    workspace_id, user_id, batch.id
                )
            elif batch.module == ImportModule.finance_transfers:
                await self.repository.delete_cash_balances_for_import(workspace_id, batch.id)
                deleted_records = await self.repository.delete_capital_transfers_for_batch(
                    workspace_id, batch.id
                )
            elif batch.module == ImportModule.investing_demat_cas:
                # Read-only verification snapshot — deleting it is always safe,
                # no holding/order/cash side effects to unwind (unlike orders).
                deleted_records = await HoldingVerificationRepository(
                    self.session
                ).delete_for_import_batch(workspace_id, batch.id)
            else:
                deleted_records = 0
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


async def run_background_validate(
    workspace_id: int,
    user_id: int,
    batch_public_id: uuid.UUID,
    file_path: str,
    file_password: str | None = None,
) -> None:
    async with postgres.async_session_maker() as session:
        repo = ImportRepository(session)
        service = ImportService(repo, session)
        audit_logger = AuditLogger(session)
        batch = None
        try:
            batch = await repo.get_by_public_id(workspace_id, batch_public_id)
            if batch:
                await service.validate_batch_file(
                    workspace_id,
                    user_id,
                    batch,
                    file_path,
                    audit_logger,
                    file_password=file_password,
                )
            await session.commit()
        except Exception:
            await session.rollback()
            if batch is not None:
                batch.status = ImportStatus.failed_validation
                batch.updated_at = datetime.now(UTC)
                await repo.save_batch(batch)
                await session.commit()
            raise
        finally:
            with contextlib.suppress(Exception):
                Path(file_path).unlink(missing_ok=True)


async def run_background_commit(
    workspace_id: int,
    user_id: int,
    batch_public_id: uuid.UUID,
) -> None:
    async with postgres.async_session_maker() as session:
        repo = ImportRepository(session)
        order_service = InvestingOrderService(
            order_repository=InvestingOrderRepository(session),
            holding_repository=HoldingRepository(session),
            cash_balance_repository=CashBalanceRepository(session),
            account_repository=AccountRepository(session),
            currency_repository=CurrencyRepository(session),
            instrument_service=InstrumentService(
                InstrumentRepository(session), CompanyRepository(session)
            ),
            lot_repository=LotRepository(session),
            corporate_action_repository=CorporateActionRepository(session),
        )
        service = ImportService(repo, session, order_service=order_service)
        audit_logger = AuditLogger(session)
        try:
            await service.commit_batch(workspace_id, user_id, batch_public_id, audit_logger)
            await session.commit()
        except Exception:
            # commit_batch already rolled back, saved failed_commit status with error
            # message, and committed that status update — don't double-rollback here,
            # but re-raise so monitoring tools can log/alert on the failure
            raise
