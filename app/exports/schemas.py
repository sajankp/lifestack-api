import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.exports.models import ExportFormat, ExportStatus

SUPPORTED_MODULES = {"todo", "spending", "investing", "health"}


class ExportCreate(BaseModel):
    format: ExportFormat
    modules: list[str] = Field(default_factory=lambda: ["todo", "spending", "investing"])

    model_config = ConfigDict(extra="forbid")


class ExportResponse(BaseModel):
    public_id: uuid.UUID
    workspace_id: int
    requested_by: int
    format: ExportFormat
    schema_version: int
    scope: dict
    status: ExportStatus
    storage_key: str | None
    artifact_mime_type: str | None
    artifact_filename: str | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None

    model_config = ConfigDict(from_attributes=True)
