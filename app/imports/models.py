import uuid
from datetime import UTC, datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class ImportModule(StrEnum):
    spending_transactions = "spending-transactions"
    spending_budgets = "spending-budgets"
    # Kept for backward-compat deserialization of historic import_batches rows only.
    investing_holdings = "investing-holdings"
    investing_constituents = "investing-constituents"
    investing_orders = "investing-orders"


class ImportStatus(StrEnum):
    uploaded = "uploaded"
    validated = "validated"
    failed_validation = "failed_validation"
    committing = "committing"
    completed = "completed"
    failed_commit = "failed_commit"


class ImportBatch(SQLModel, table=True):
    __tablename__ = "import_batches"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, unique=True, index=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    module: ImportModule = Field(sa_type=sa.String(length=64), index=True)
    status: ImportStatus = Field(
        default=ImportStatus.uploaded, sa_type=sa.String(length=32), index=True
    )

    filename: str = Field(max_length=255)
    content_type: str | None = Field(default=None, max_length=100)
    file_size_bytes: int = Field(default=0)
    file_sha256: str = Field(max_length=64)

    storage_backend: str = Field(default="none", max_length=16)
    storage_key: str | None = Field(default=None, max_length=512)

    total_rows: int = Field(default=0)
    valid_rows: int = Field(default=0)
    error_rows: int = Field(default=0)

    started_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    validated_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    committed_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )


class ImportError(SQLModel, table=True):
    __tablename__ = "import_errors"

    id: int | None = Field(default=None, primary_key=True)
    import_batch_id: int = Field(foreign_key="import_batches.id", index=True)
    row_number: int = Field(index=True)
    field_name: str | None = Field(default=None, max_length=100)
    error_code: str = Field(max_length=64)
    message: str = Field(max_length=1000)
    raw_value: str | None = Field(default=None, max_length=500)


class ImportPreviewRow(SQLModel, table=True):
    __tablename__ = "import_preview_rows"

    id: int | None = Field(default=None, primary_key=True)
    import_batch_id: int = Field(foreign_key="import_batches.id", index=True)
    row_number: int = Field(index=True)
    payload_json: dict = Field(default_factory=dict, sa_type=sa.JSON)
