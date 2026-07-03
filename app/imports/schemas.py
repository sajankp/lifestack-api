import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.imports.models import ImportModule, ImportStatus

TEMPLATE_HEADERS: dict[ImportModule, list[str]] = {
    ImportModule.spending_transactions: [
        "occurred_at",
        "type",
        "amount",
        "category",
        "description",
        "account_name",
    ],
    ImportModule.spending_budgets: [
        "month_start",
        "category",
        "amount",
    ],
    ImportModule.investing_constituents: [
        "instrument_symbol",
        "company_name",
        "company_ticker",
        "weight",
        "as_of_date",
    ],
    ImportModule.investing_orders: [
        "order_type",
        "symbol",
        "instrument_type",
        "instrument_name",
        "account_name",
        "quantity",
        "price_per_unit",
        "currency",
        "brokerage_fee",
        "tax_amount",
        "other_fees",
        "occurred_at",
        "exchange_name",
        "notes",
    ],
    ImportModule.finance_transfers: [
        "occurred_at",
        "from_account",
        "to_account",
        "from_currency",
        "to_currency",
        "gross_amount",
        "net_amount_received",
        "notes",
        "from_module",
        "to_module",
        "fx_rate_used",
        "fx_fee_amount",
        "platform_fee_amount",
        "tax_amount",
    ],
}

SPENDEE_TRANSACTION_HEADERS = [
    "Date",
    "Wallet",
    "Type",
    "Category name",
    "Amount",
    "Currency",
    "Note",
    "Labels",
]


class ImportErrorResponse(BaseModel):
    row_number: int
    field_name: str | None = None
    error_code: str
    message: str
    raw_value: str | None = None

    model_config = ConfigDict(from_attributes=True)


class ImportBatchResponse(BaseModel):
    public_id: uuid.UUID
    module: ImportModule
    status: ImportStatus
    filename: str
    content_type: str | None
    file_size_bytes: int
    file_sha256: str
    storage_backend: str
    storage_key: str | None
    total_rows: int
    valid_rows: int
    error_rows: int
    commit_error: str | None = None
    started_at: datetime
    validated_at: datetime | None
    committed_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class ImportErrorSummary(BaseModel):
    total_errors: int
    returned_errors: int
    by_code: dict[str, int]
    by_field: dict[str, int]


class ImportPreviewRowResponse(BaseModel):
    row_number: int
    payload_json: dict

    model_config = ConfigDict(from_attributes=True)


class ImportValidateResponse(BaseModel):
    import_batch: ImportBatchResponse
    errors: list[ImportErrorResponse]
    error_summary: ImportErrorSummary
    preview_rows: list[ImportPreviewRowResponse] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)
    corporate_action_suspected: list[dict] = Field(default_factory=list)


class ImportCommitResponse(BaseModel):
    import_batch: ImportBatchResponse
    inserted_rows: int
    auto_created_categories: list[str] = Field(default_factory=list)
    auto_created_category_count: int = 0
