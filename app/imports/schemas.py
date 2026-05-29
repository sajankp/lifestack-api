import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.imports.models import ImportModule, ImportStatus

TEMPLATE_HEADERS: dict[ImportModule, list[str]] = {
    ImportModule.spending_transactions: [
        "occurred_at",
        "type",
        "amount",
        "category",
        "description",
    ],
    ImportModule.spending_budgets: [
        "month_start",
        "category",
        "amount",
    ],
    ImportModule.investing_holdings: [
        "symbol",
        "account_name",
        "quantity",
        "avg_cost",
        "currency",
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
    "Author",
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
    started_at: datetime
    validated_at: datetime | None
    committed_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class ImportValidateResponse(BaseModel):
    import_batch: ImportBatchResponse
    errors: list[ImportErrorResponse]


class ImportCommitResponse(BaseModel):
    import_batch: ImportBatchResponse
    inserted_rows: int
