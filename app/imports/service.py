import asyncio
import contextlib
import csv
import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import openpyxl
from fastapi import UploadFile
from sqlalchemy import delete, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.audit import AuditLogger
from app.core.database import postgres
from app.core.exceptions import NotFoundError, ValidationError
from app.finance.models import Account, CapitalTransfer, Currency, TransferModule
from app.finance.repository import AccountRepository, CurrencyRepository
from app.imports.models import (
    ImportBatch,
    ImportError,
    ImportModule,
    ImportPreviewRow,
    ImportStatus,
)
from app.imports.repository import ImportRepository
from app.imports.schemas import SPENDEE_TRANSACTION_HEADERS, TEMPLATE_HEADERS
from app.investing.models import Company, Instrument, InstrumentConstituent, InstrumentType
from app.investing.repository import (
    CashBalanceRepository,
    CompanyRepository,
    HoldingRepository,
    InstrumentRepository,
    InvestingOrderRepository,
)
from app.investing.schemas import InvestingOrderCreate
from app.investing.service import InstrumentService, InvestingOrderService
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

    def template_csv(self, module: ImportModule) -> str:
        header = TEMPLATE_HEADERS[module]
        lines = [",".join(header)]
        if module == ImportModule.spending_transactions:
            lines.append("2026-05-01T09:30:00Z,expense,42.50,Food & Dining,Breakfast")
        elif module == ImportModule.spending_budgets:
            lines.append("2026-05-01,Food & Dining,800.00")
        elif module == ImportModule.investing_constituents:
            lines.append("UMMA,Apple Inc,AAPL,0.082,2026-06-14")
        elif module == ImportModule.investing_orders:
            lines.append(
                "buy,AAPL,Primary Brokerage,10,150.00,USD,1.99,0,0,2026-01-15T10:30:00+00:00,NASDAQ,First purchase"
            )
        elif module == ImportModule.finance_transfers:
            lines.append(
                "2026-05-01T09:30:00Z,ICICI,GROWW,INR,INR,50000.00,50000.00,SIP Investment"
            )
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
    ) -> tuple[ImportBatch, str]:
        if module == ImportModule.investing_holdings:
            raise ValidationError(detail="investing-holdings imports are no longer supported")
        if not upload.filename:
            raise ValidationError(detail="filename is required")
        if not (
            upload.filename.lower().endswith(".csv") or upload.filename.lower().endswith(".xlsx")
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
    ) -> tuple[ImportBatch, list[ImportError]]:
        by_name, by_public = await self._category_maps(workspace_id)
        account_map = await self._account_map(workspace_id)
        currency_set = await self._currency_set()

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
                    if header_mode == "spendee":
                        occurred_raw = self._norm(row.get("Date"))
                        raw_type = self._norm(row.get("Type"))
                        type_raw = raw_type.lower() if raw_type else ""
                        amount_raw = self._norm(row.get("Amount"))
                        category_raw = self._norm(row.get("Category name"))
                        description_raw = self._norm(row.get("Note")) or None
                        account_name_raw = self._norm(row.get("Wallet")) or None
                        labels_raw = self._norm(row.get("Labels")) or None
                    else:
                        occurred_raw = self._norm(row.get("occurred_at"))
                        raw_type = self._norm(row.get("type"))
                        type_raw = raw_type.lower() if raw_type else ""
                        amount_raw = self._norm(row.get("amount"))
                        category_raw = self._norm(row.get("category"))
                        description_raw = self._norm(row.get("description")) or None
                        account_name_raw = self._norm(row.get("account_name")) or None
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

                    account_id = None
                    if account_name_raw:
                        account_id = account_map.get(account_name_raw.lower())
                        if account_id is None:
                            add_error(
                                "account_name",
                                "not_found",
                                "account not found in workspace",
                                account_name_raw,
                            )
                    elif header_mode == "spendee":
                        add_error(
                            "account_name",
                            "required",
                            "Wallet is required and must match an existing account in the workspace",
                            account_name_raw,
                        )

                    payload = {
                        "occurred_at": occurred_at.isoformat() if occurred_at else None,
                        "type": type_raw,
                        "amount": str(amount) if amount is not None else None,
                        "category_id": category_id,
                        "category_name": category_raw if category_raw else None,
                        "description": description_raw,
                        "account_name": account_name_raw,
                        "account_id": account_id,
                        "labels": labels_raw,
                    }
                elif batch.module == ImportModule.spending_budgets:
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
                        "category_name": category_raw if category_raw else None,
                        "amount": str(amount) if amount is not None else None,
                    }
                elif batch.module == ImportModule.investing_orders:
                    order_type_raw = self._norm(row.get("order_type"))
                    symbol_raw = self._norm(row.get("symbol"))
                    account_name_raw = self._norm(row.get("account_name"))
                    quantity_raw = self._norm(row.get("quantity"))
                    price_raw = self._norm(row.get("price_per_unit"))
                    currency_raw = self._norm(row.get("currency"))
                    occurred_raw = self._norm(row.get("occurred_at"))
                    fee_raw = self._norm(row.get("brokerage_fee")) or "0"
                    tax_raw = self._norm(row.get("tax_amount")) or "0"
                    other_raw = self._norm(row.get("other_fees")) or "0"
                    exchange_raw = self._norm(row.get("exchange_name")) or None
                    notes_raw = self._norm(row.get("notes")) or None

                    order_type_val = order_type_raw.lower() if order_type_raw else ""
                    if order_type_val not in {"buy", "sell"}:
                        add_error(
                            "order_type",
                            "invalid_enum",
                            "order_type must be 'buy' or 'sell'",
                            order_type_raw,
                        )

                    if not symbol_raw:
                        add_error("symbol", "required", "symbol is required", symbol_raw)

                    account_public_id = None
                    if account_name_raw:
                        account_public_id = order_account_pub_map.get(account_name_raw.lower())
                        if account_public_id is None:
                            add_error(
                                "account_name",
                                "not_found",
                                "account not found in workspace",
                                account_name_raw,
                            )
                    else:
                        add_error(
                            "account_name", "required", "account_name is required", account_name_raw
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
                        price = Decimal(price_raw)
                        if price <= 0:
                            raise InvalidOperation
                    except Exception:
                        add_error(
                            "price_per_unit",
                            "invalid_decimal",
                            "price_per_unit must be a positive decimal",
                            price_raw,
                        )
                        price = None

                    currency = None
                    if currency_raw:
                        currency = currency_raw.upper()
                        if currency not in currency_set:
                            add_error(
                                "currency",
                                "not_found",
                                "currency not enabled in workspace",
                                currency_raw,
                            )
                    else:
                        add_error("currency", "required", "currency is required", currency_raw)

                    occurred_at = None
                    try:
                        occurred_at = datetime.fromisoformat(occurred_raw.replace("Z", "+00:00"))
                    except Exception:
                        add_error(
                            "occurred_at",
                            "invalid_datetime",
                            "occurred_at must be ISO datetime",
                            occurred_raw,
                        )

                    def _safe_decimal(raw: str, field: str) -> Decimal:
                        try:
                            val = Decimal(raw)
                            if val < 0:
                                raise InvalidOperation
                            return val
                        except Exception:
                            add_error(
                                field,
                                "invalid_decimal",
                                f"{field} must be a non-negative decimal",
                                raw,
                            )
                            return Decimal("0")

                    fee = _safe_decimal(fee_raw, "brokerage_fee")
                    tax = _safe_decimal(tax_raw, "tax_amount")
                    other = _safe_decimal(other_raw, "other_fees")

                    payload = {
                        "order_type": order_type_val,
                        "symbol": symbol_raw.upper() if symbol_raw else None,
                        "account_name": account_name_raw,
                        "account_public_id": str(account_public_id) if account_public_id else None,
                        "quantity": str(quantity) if quantity is not None else None,
                        "price_per_unit": str(price) if price is not None else None,
                        "currency": currency,
                        "brokerage_fee": str(fee),
                        "tax_amount": str(tax),
                        "other_fees": str(other),
                        "occurred_at": occurred_at.isoformat() if occurred_at else None,
                        "exchange_name": exchange_raw,
                        "notes": notes_raw,
                    }
                elif batch.module == ImportModule.finance_transfers:
                    occurred_raw = self._norm(row.get("occurred_at"))
                    from_account_raw = self._norm(row.get("from_account"))
                    to_account_raw = self._norm(row.get("to_account"))
                    from_currency_raw = self._norm(row.get("from_currency"))
                    to_currency_raw = self._norm(row.get("to_currency"))
                    gross_amount_raw = self._norm(row.get("gross_amount"))
                    net_amount_raw = self._norm(row.get("net_amount_received"))
                    notes_raw = self._norm(row.get("notes")) or None
                    from_module_raw = self._norm(row.get("from_module")) or "spending"
                    to_module_raw = self._norm(row.get("to_module")) or "investing"
                    fx_rate_raw = self._norm(row.get("fx_rate_used")) or None
                    fx_fee_raw = self._norm(row.get("fx_fee_amount")) or "0"
                    platform_fee_raw = self._norm(row.get("platform_fee_amount")) or "0"
                    tax_raw = self._norm(row.get("tax_amount")) or "0"

                    occurred_at = None
                    try:
                        occurred_at = datetime.fromisoformat(occurred_raw.replace("Z", "+00:00"))
                    except Exception:
                        add_error(
                            "occurred_at",
                            "invalid_datetime",
                            "occurred_at must be ISO datetime",
                            occurred_raw,
                        )

                    from_account_pub_id = None
                    if from_account_raw:
                        from_account_pub_id = order_account_pub_map.get(from_account_raw.lower())
                        if from_account_pub_id is None:
                            add_error(
                                "from_account",
                                "not_found",
                                "from_account not found in workspace",
                                from_account_raw,
                            )
                    else:
                        add_error(
                            "from_account", "required", "from_account is required", from_account_raw
                        )

                    to_account_pub_id = None
                    if to_account_raw:
                        to_account_pub_id = order_account_pub_map.get(to_account_raw.lower())
                        if to_account_pub_id is None:
                            add_error(
                                "to_account",
                                "not_found",
                                "to_account not found in workspace",
                                to_account_raw,
                            )
                    else:
                        add_error(
                            "to_account", "required", "to_account is required", to_account_raw
                        )

                    from_currency = None
                    if from_currency_raw:
                        from_currency = from_currency_raw.upper()
                        if from_currency not in currency_set:
                            add_error(
                                "from_currency",
                                "not_found",
                                "from_currency not enabled in workspace",
                                from_currency_raw,
                            )
                    else:
                        add_error(
                            "from_currency",
                            "required",
                            "from_currency is required",
                            from_currency_raw,
                        )

                    to_currency = None
                    if to_currency_raw:
                        to_currency = to_currency_raw.upper()
                        if to_currency not in currency_set:
                            add_error(
                                "to_currency",
                                "not_found",
                                "to_currency not enabled in workspace",
                                to_currency_raw,
                            )
                    else:
                        add_error(
                            "to_currency", "required", "to_currency is required", to_currency_raw
                        )

                    try:
                        gross_amount = Decimal(gross_amount_raw)
                        if gross_amount < 0:
                            raise InvalidOperation
                    except Exception:
                        add_error(
                            "gross_amount",
                            "invalid_decimal",
                            "gross_amount must be a non-negative decimal",
                            gross_amount_raw,
                        )
                        gross_amount = None

                    try:
                        net_amount_received = Decimal(net_amount_raw)
                        if net_amount_received < 0:
                            raise InvalidOperation
                    except Exception:
                        add_error(
                            "net_amount_received",
                            "invalid_decimal",
                            "net_amount_received must be a non-negative decimal",
                            net_amount_raw,
                        )
                        net_amount_received = None

                    fx_rate_used = None
                    if fx_rate_raw:
                        try:
                            fx_rate_used = Decimal(fx_rate_raw)
                            if fx_rate_used <= 0:
                                raise InvalidOperation
                        except Exception:
                            add_error(
                                "fx_rate_used",
                                "invalid_decimal",
                                "fx_rate_used must be a positive decimal",
                                fx_rate_raw,
                            )

                    def _safe_decimal(raw: str, field: str) -> Decimal:
                        try:
                            val = Decimal(raw)
                            if val < 0:
                                raise InvalidOperation
                            return val
                        except Exception:
                            add_error(
                                field,
                                "invalid_decimal",
                                f"{field} must be a non-negative decimal",
                                raw,
                            )
                            return Decimal("0")

                    fx_fee_amount = _safe_decimal(fx_fee_raw, "fx_fee_amount")
                    platform_fee_amount = _safe_decimal(platform_fee_raw, "platform_fee_amount")
                    tax_amount = _safe_decimal(tax_raw, "tax_amount")

                    if from_module_raw not in {"spending", "investing"}:
                        add_error(
                            "from_module",
                            "invalid_enum",
                            "from_module must be spending or investing",
                            from_module_raw,
                        )
                    if to_module_raw not in {"spending", "investing"}:
                        add_error(
                            "to_module",
                            "invalid_enum",
                            "to_module must be spending or investing",
                            to_module_raw,
                        )

                    if gross_amount is not None and net_amount_received is not None:
                        if (
                            from_currency == to_currency
                            and fx_rate_used is not None
                            and fx_rate_used != Decimal("1")
                        ):
                            add_error(
                                "fx_rate_used",
                                "invalid_value",
                                "FX rate must be 1.0 when transferring between the same currency",
                                str(fx_rate_used),
                            )

                        gross = gross_amount
                        fx_rate = fx_rate_used if fx_rate_used is not None else Decimal("1")
                        converted_gross = gross * fx_rate
                        total_fees = fx_fee_amount + platform_fee_amount + tax_amount
                        net = net_amount_received
                        difference = abs(converted_gross - total_fees - net)
                        if difference > Decimal("0.01"):
                            add_error(
                                "net_amount_received",
                                "invalid_value",
                                f"Transfer arithmetic inconsistent: gross ({gross:.2f}) * rate ({fx_rate:.4f}) - fees ({total_fees:.2f}) ≠ net ({net:.2f}). Difference: {difference:.4f}",
                                str(net_amount_received),
                            )

                    payload = {
                        "occurred_at": occurred_at.isoformat() if occurred_at else None,
                        "from_account": from_account_raw,
                        "from_account_public_id": str(from_account_pub_id)
                        if from_account_pub_id
                        else None,
                        "to_account": to_account_raw,
                        "to_account_public_id": str(to_account_pub_id)
                        if to_account_pub_id
                        else None,
                        "from_currency": from_currency,
                        "to_currency": to_currency,
                        "gross_amount": str(gross_amount) if gross_amount is not None else None,
                        "net_amount_received": str(net_amount_received)
                        if net_amount_received is not None
                        else None,
                        "notes": notes_raw,
                        "from_module": from_module_raw,
                        "to_module": to_module_raw,
                        "fx_rate_used": str(fx_rate_used) if fx_rate_used is not None else None,
                        "fx_fee_amount": str(fx_fee_amount),
                        "platform_fee_amount": str(platform_fee_amount),
                        "tax_amount": str(tax_amount),
                    }
                else:
                    instrument_symbol_raw = self._norm(row.get("instrument_symbol"))
                    company_name_raw = self._norm(row.get("company_name"))
                    company_ticker_raw = self._norm(row.get("company_ticker"))
                    weight_raw = self._norm(row.get("weight"))
                    as_of_date_raw = self._norm(row.get("as_of_date"))

                    inst = None
                    if instrument_symbol_raw:
                        inst = instruments_map.get(instrument_symbol_raw.upper())
                        if (
                            not inst
                            or not inst.is_active
                            or inst.instrument_type
                            not in {InstrumentType.etf.value, InstrumentType.mutual_fund.value}
                        ):
                            add_error(
                                "instrument_symbol",
                                "invalid_instrument",
                                "instrument_symbol must resolve to an active ETF/Mutual Fund instrument in the current workspace",
                                instrument_symbol_raw,
                            )
                    else:
                        add_error(
                            "instrument_symbol",
                            "required",
                            "instrument_symbol is required",
                            instrument_symbol_raw,
                        )

                    if not company_name_raw:
                        add_error(
                            "company_name",
                            "required",
                            "company_name is required",
                            company_name_raw,
                        )

                    try:
                        weight = Decimal(weight_raw)
                        if not (Decimal("0.00000001") <= weight <= Decimal("1.0")):
                            raise InvalidOperation
                    except Exception:
                        add_error(
                            "weight",
                            "invalid_decimal",
                            "weight must be a positive decimal between 0 and 1",
                            weight_raw,
                        )
                        weight = None

                    try:
                        as_of_date = datetime.strptime(as_of_date_raw, "%Y-%m-%d").date()
                    except Exception:
                        add_error(
                            "as_of_date",
                            "invalid_date",
                            "as_of_date must be YYYY-MM-DD",
                            as_of_date_raw,
                        )
                        as_of_date = None

                    payload = {
                        "instrument_symbol": instrument_symbol_raw.upper()
                        if instrument_symbol_raw
                        else None,
                        "instrument_id": inst.id if inst else None,
                        "company_name": company_name_raw,
                        "company_ticker": company_ticker_raw or None,
                        "weight": str(weight) if weight is not None else None,
                        "as_of_date": as_of_date.isoformat() if as_of_date else None,
                        "source": "csv_import",
                    }

                    if (
                        instrument_symbol_raw
                        and inst
                        and inst.is_active
                        and inst.instrument_type
                        in {InstrumentType.etf.value, InstrumentType.mutual_fund.value}
                        and weight is not None
                        and as_of_date is not None
                    ):
                        key = (instrument_symbol_raw.upper(), as_of_date_raw)
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

            for (sym, dt_str), weights in weight_groups.items():
                total_w = sum(weights)
                if not (Decimal("0.99") <= total_w <= Decimal("1.01")):
                    errors.append(
                        ImportError(
                            import_batch_id=batch.id,
                            row_number=1,
                            field_name="weight",
                            error_code="invalid_weight_sum",
                            message=f"Total weight for instrument '{sym}' on date '{dt_str}' is {total_w}, which is outside the range 0.99 - 1.01.",
                            raw_value=str(total_w),
                        )
                    )

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

        inserted = 0
        auto_created_categories: list[str] = []
        try:
            if batch.module == ImportModule.investing_constituents:
                # 1. Fetch all preview rows to extract unique target snapshots (instrument_id, as_of_date)
                preview_rows = await self.repository.iter_preview_rows(batch.id)
                unique_snapshots = set()
                for row in preview_rows:
                    p = row.payload_json
                    inst_id = p.get("instrument_id")
                    as_of_date_str = p.get("as_of_date")
                    if inst_id is not None and as_of_date_str:
                        unique_snapshots.add((
                            int(inst_id),
                            datetime.strptime(as_of_date_str, "%Y-%m-%d").date(),
                        ))

                # 2. Delete existing snapshot records under source "csv_import"
                for inst_id, as_of_date in unique_snapshots:
                    await self.session.execute(
                        delete(InstrumentConstituent).where(
                            InstrumentConstituent.instrument_id == inst_id,
                            InstrumentConstituent.as_of_date == as_of_date,
                            InstrumentConstituent.source == "csv_import",
                        )
                    )

                # 3. Cache existing workspace companies by lowercased name
                company_rows = (
                    (
                        await self.session.execute(
                            select(Company).where(Company.workspace_id == workspace_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                company_cache = {c.name.strip().lower(): c for c in company_rows}

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
                            account_id=p.get("account_id"),
                            labels=p.get("labels"),
                            source_type=TransactionSourceType.imported,
                            source_import_id=batch.id,
                            source_ref=f"{batch.public_id}:{row.row_number}",
                        )
                        self.session.add(tx)
                        inserted += 1
                elif batch.module == ImportModule.spending_budgets:
                    budget_keys = {
                        (
                            int(row.payload_json["category_id"]),
                            datetime.fromisoformat(row.payload_json["month_start"]).date(),
                        )
                        for row in rows
                    }
                    existing_budgets = {}
                    if budget_keys:
                        budget_rows = (
                            (
                                await self.session.execute(
                                    select(SpendingBudget).where(
                                        SpendingBudget.workspace_id == workspace_id,
                                        tuple_(
                                            SpendingBudget.category_id,
                                            SpendingBudget.month_start,
                                        ).in_(budget_keys),
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                        existing_budgets = {
                            (budget.category_id, budget.month_start): budget
                            for budget in budget_rows
                        }

                    for row in rows:
                        p = row.payload_json
                        month_start_date = datetime.fromisoformat(p["month_start"]).date()
                        budget_key = (int(p["category_id"]), month_start_date)
                        existing_budget = existing_budgets.get(budget_key)

                        if existing_budget:
                            existing_budget.amount = Decimal(p["amount"])
                            existing_budget.source_type = "imported"
                            existing_budget.source_import_id = batch.id
                            existing_budget.source_ref = f"{batch.public_id}:{row.row_number}"
                            existing_budget.updated_at = datetime.now(UTC)
                        else:
                            budget = SpendingBudget(
                                workspace_id=workspace_id,
                                category_id=int(p["category_id"]),
                                amount=Decimal(p["amount"]),
                                month_start=month_start_date,
                                source_type="imported",
                                source_import_id=batch.id,
                                source_ref=f"{batch.public_id}:{row.row_number}",
                            )
                            self.session.add(budget)
                            existing_budgets[budget_key] = budget
                        inserted += 1
                elif batch.module == ImportModule.investing_orders:
                    if self.order_service is None:
                        raise ValidationError(
                            detail="Order service is not available for this import type"
                        )
                    order_ins: list[InvestingOrderCreate] = []
                    for row in rows:
                        p = row.payload_json
                        required = (
                            "order_type",
                            "symbol",
                            "account_public_id",
                            "quantity",
                            "price_per_unit",
                            "currency",
                            "occurred_at",
                        )
                        if any(p.get(f) is None for f in required):
                            raise ValidationError(
                                detail=f"Row {row.row_number}: missing required order field in preview payload"
                            )
                        order_ins.append(
                            InvestingOrderCreate(
                                account_id=uuid.UUID(p["account_public_id"]),
                                order_type=p["order_type"],
                                symbol=p["symbol"],
                                quantity=Decimal(p["quantity"]),
                                price_per_unit=Decimal(p["price_per_unit"]),
                                currency=p["currency"],
                                brokerage_fee=Decimal(p.get("brokerage_fee") or "0"),
                                tax_amount=Decimal(p.get("tax_amount") or "0"),
                                other_fees=Decimal(p.get("other_fees") or "0"),
                                exchange_name=p.get("exchange_name"),
                                occurred_at=datetime.fromisoformat(p["occurred_at"]),
                                notes=p.get("notes"),
                            )
                        )
                    created = await self.order_service.bulk_import_orders(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        orders=order_ins,
                        source_import_id=batch.id,
                        audit_logger=audit_logger,
                    )
                    inserted += len(created)
                elif batch.module == ImportModule.finance_transfers:
                    account_map = await self._account_map(workspace_id)
                    for row in rows:
                        p = row.payload_json
                        from_account_id = account_map.get(p["from_account"].lower())
                        to_account_id = account_map.get(p["to_account"].lower())
                        if from_account_id is None or to_account_id is None:
                            raise ValidationError(
                                detail="from_account or to_account not found in workspace"
                            )

                        transfer = CapitalTransfer(
                            workspace_id=workspace_id,
                            actor_id=user_id,
                            from_module=TransferModule(p["from_module"]),
                            to_module=TransferModule(p["to_module"]),
                            from_account_id=from_account_id,
                            to_account_id=to_account_id,
                            from_currency_code=p["from_currency"],
                            to_currency_code=p["to_currency"],
                            gross_amount=Decimal(p["gross_amount"]),
                            fx_rate_used=Decimal(p.get("fx_rate_used"))
                            if p.get("fx_rate_used")
                            else None,
                            fx_fee_amount=Decimal(p.get("fx_fee_amount") or "0"),
                            platform_fee_amount=Decimal(p.get("platform_fee_amount") or "0"),
                            tax_amount=Decimal(p.get("tax_amount") or "0"),
                            net_amount_received=Decimal(p["net_amount_received"]),
                            occurred_at=datetime.fromisoformat(p["occurred_at"]),
                            notes=p.get("notes"),
                            source_type="imported",
                            source_import_id=batch.id,
                            source_ref=f"{batch.public_id}:{row.row_number}",
                        )
                        self.session.add(transfer)
                        inserted += 1
                else:  # ImportModule.investing_constituents
                    # Pre-load existing constituents for the chunk to prevent unique constraint violation
                    keys = []
                    for row in rows:
                        p = row.payload_json
                        company_name_raw = p.get("company_name")
                        company_name_norm = (
                            company_name_raw.strip().lower() if company_name_raw else ""
                        )
                        company = company_cache.get(company_name_norm)
                        if company is not None and p.get("instrument_id") is not None:
                            try:
                                dt = datetime.strptime(p["as_of_date"], "%Y-%m-%d").date()
                                keys.append((int(p["instrument_id"]), company.id, dt))
                            except Exception:
                                pass

                    existing_consts = {}
                    if keys:
                        const_rows = (
                            (
                                await self.session.execute(
                                    select(InstrumentConstituent).where(
                                        InstrumentConstituent.source == "csv_import",
                                        tuple_(
                                            InstrumentConstituent.instrument_id,
                                            InstrumentConstituent.constituent_company_id,
                                            InstrumentConstituent.as_of_date,
                                        ).in_(keys),
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                        existing_consts = {
                            (c.instrument_id, c.constituent_company_id, c.as_of_date): c
                            for c in const_rows
                        }

                    for row in rows:
                        p = row.payload_json
                        company_name_raw = p.get("company_name")
                        company_name_norm = (
                            company_name_raw.strip().lower() if company_name_raw else ""
                        )

                        company = company_cache.get(company_name_norm)
                        if company is None:
                            company = Company(
                                workspace_id=workspace_id,
                                name=company_name_raw,
                                ticker=p.get("company_ticker") or None,
                            )
                            self.session.add(company)
                            await self.session.flush()
                            company_cache[company_name_norm] = company

                        instrument_id = p.get("instrument_id")
                        if instrument_id is None:
                            raise ValidationError(
                                detail="Instrument ID is missing in preview payload"
                            )

                        as_of_date = datetime.strptime(p["as_of_date"], "%Y-%m-%d").date()
                        const_key = (int(instrument_id), company.id, as_of_date)
                        existing_const = existing_consts.get(const_key)

                        if existing_const:
                            existing_const.weight = Decimal(p["weight"])
                            existing_const.fetched_at = datetime.now(UTC)
                        else:
                            constituent = InstrumentConstituent(
                                instrument_id=int(instrument_id),
                                constituent_company_id=company.id,
                                weight=Decimal(p["weight"]),
                                as_of_date=as_of_date,
                                source="csv_import",
                                fetched_at=datetime.now(UTC),
                            )
                            self.session.add(constituent)
                            existing_consts[const_key] = constituent
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
        except Exception as e:
            await self.session.rollback()
            batch = await self.repository.get_by_public_id(workspace_id, import_public_id)
            if not batch:
                raise
            batch.status = ImportStatus.failed_commit
            batch.updated_at = datetime.now(UTC)
            await self.repository.save_batch(batch)
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

        Placing orders has side effects (cash-balance snapshots and holding
        avg_cost/quantity changes), so deleting the order rows alone would orphan
        cash balances and leave holdings incorrect. This removes the order-triggered
        cash balances and recomputes the affected holdings from the remaining orders.
        """
        if self.order_service is None:
            raise ValidationError(detail="Order service is required to roll back an order import")

        orders = await self.repository.list_investing_orders_for_batch(
            workspace_id, import_batch_id
        )
        if not orders:
            return 0

        affected = {(o.symbol, o.account_id) for o in orders}
        await self.repository.delete_cash_balances_by_trigger_refs(
            workspace_id, "order", [o.public_id for o in orders]
        )
        deleted_records = await self.repository.delete_investing_orders_for_batch(
            workspace_id, import_batch_id
        )
        for symbol, account_id in affected:
            await self.order_service._recompute_holding_from_orders(
                workspace_id, user_id, symbol, account_id
            )
        return deleted_records

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
            elif batch.module == ImportModule.investing_orders:
                deleted_records = await self._rollback_investing_orders(
                    workspace_id, user_id, batch.id
                )
            elif batch.module == ImportModule.finance_transfers:
                deleted_records = await self.repository.delete_capital_transfers_for_batch(
                    workspace_id, batch.id
                )
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
                    workspace_id, user_id, batch, file_path, audit_logger
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
        )
        service = ImportService(repo, session, order_service=order_service)
        audit_logger = AuditLogger(session)
        try:
            await service.commit_batch(workspace_id, user_id, batch_public_id, audit_logger)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
