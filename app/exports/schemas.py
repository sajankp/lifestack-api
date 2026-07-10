import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.exports.models import ExportFormat, ExportStatus

# The single source of truth for what each export module contains (spec-070).
# The web UI and the OpenAPI-exposed GET /exports/modules are both derived from
# this map so backend and frontend can never disagree on which modules exist.
# Investing exports the authoritative order history (spec-041) alongside the
# derived holdings snapshot; finance carries accounts + capital transfers so an
# export is self-consistent and round-trips what import can create.
EXPORT_MODULES: dict[str, list[str]] = {
    "todo": ["todos", "recurring_rules"],
    "spending": [
        "category_groups",
        "categories",
        "transactions",
        "budgets",
        "recurring_transactions",
    ],
    "investing": ["holdings", "cash_balances", "orders", "order_lots", "corporate_actions"],
    "finance": ["accounts", "capital_transfers", "finance_settings", "workspace_currencies"],
    "health": ["medications", "medication_events", "weight_entries"],
}

SUPPORTED_MODULES = set(EXPORT_MODULES)


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
