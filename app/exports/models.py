import uuid
from datetime import UTC, datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class ExportFormat(StrEnum):
    json = "json"
    csv = "csv"


class ExportStatus(StrEnum):
    pending = "pending"
    ready = "ready"
    failed = "failed"
    expired = "expired"


class ExportRecord(SQLModel, table=True):
    __tablename__ = "exports"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    requested_by: int = Field(foreign_key="users.id", index=True)
    format: ExportFormat = Field(sa_type=sa.String(length=16))
    schema_version: int = Field(default=1)
    scope: dict = Field(default_factory=dict, sa_type=sa.JSON)
    status: ExportStatus = Field(default=ExportStatus.pending, sa_type=sa.String(length=16))
    storage_key: str | None = Field(default=None, max_length=255)
    artifact_blob: bytes | None = Field(default=None)
    artifact_mime_type: str | None = Field(default=None, max_length=100)
    artifact_filename: str | None = Field(default=None, max_length=255)
    error_message: str | None = Field(default=None, max_length=1000)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True)
    )
    completed_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
