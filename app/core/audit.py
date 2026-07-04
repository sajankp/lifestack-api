import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field, SQLModel

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Snapshot Helper
# ---------------------------------------------------------------------------


def snapshot_columns(entity: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    """Build an audit snapshot dict from an ORM entity for the given fields.

    Decimals/UUIDs/datetimes are left as-is; AuditLogger handles serialization.
    """
    return {name: getattr(entity, name) for name in fields}


# ---------------------------------------------------------------------------
# Redaction Layer
# ---------------------------------------------------------------------------

SENSITIVE_KEYS = {
    "password",
    "hashed_password",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "api_key",
    "apikey",
    "card_number",
    "ssn",
    "cvv",
    "pin",
    "credit_card",
    "credentials",
    "auth",
    "account_number",
}


def redact_details(data: Any) -> Any:
    """Recursively redact sensitive keys from dictionaries/lists case-insensitively."""
    if isinstance(data, dict):
        redacted = {}
        for key, value in data.items():
            key_lower = key.lower()
            if key_lower in SENSITIVE_KEYS:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_details(value)
        return redacted
    elif isinstance(data, list):
        return [redact_details(item) for item in data]
    return data


# ---------------------------------------------------------------------------
# Database Model
# ---------------------------------------------------------------------------


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_logs"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    actor_id: int = Field(foreign_key="users.id", index=True)

    action: str = Field(max_length=50)
    module: str = Field(max_length=50)
    entity_type: str = Field(max_length=50)
    entity_id: int = Field(index=True)

    # sa_type=sa.JSON automatically maps to JSONB in Postgres
    details: dict[str, Any] = Field(default_factory=dict, sa_type=sa.JSON)

    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True), index=True
    )


# ---------------------------------------------------------------------------
# Logger Helper
# ---------------------------------------------------------------------------


class AuditLogger:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def log(
        self,
        workspace_id: int,
        actor_id: int,
        action: str,
        module: str,
        entity_type: str,
        entity_id: int,
        details: dict[str, Any],
    ) -> AuditLog:
        """Create, validate, redact, and persist an append-only audit log entry."""
        # 1. Enforce Event Contract (Stage 1 Minimum)
        required_keys = {"entity_public_id", "before", "after", "changed_fields"}
        for key in required_keys:
            if key not in details:
                raise ValueError(f"Missing required audit detail key: {key}")

        # 2. Action-level validation
        before = details.get("before")
        after = details.get("after")

        if action == "create":
            if before is not None:
                raise ValueError("Create action must have before = null")
            if after is None:
                raise ValueError("Create action must have after != null")
        elif action in ("update", "complete"):
            if before is None or after is None:
                raise ValueError(f"{action} action must have both before and after != null")
        elif action == "delete":
            if before is None:
                raise ValueError("Delete action must have before != null")
            if after is not None:
                raise ValueError("Delete action must have after = null")

        # 3. Inject request_id if available and not already provided
        if details.get("request_id") is None:
            ctx = structlog.contextvars.get_contextvars()
            details["request_id"] = ctx.get("request_id")

        # 4. Apply PII and secrets redaction layer
        redacted_details = redact_details(details)

        # 5. Persist the log
        audit_log = AuditLog(
            workspace_id=workspace_id,
            actor_id=actor_id,
            action=action,
            module=module,
            entity_type=entity_type,
            entity_id=entity_id,
            details=redacted_details,
        )

        self.session.add(audit_log)
        await self.session.flush()

        logger.info(
            "audit_log_written",
            module=module,
            action=action,
            entity_type=entity_type,
            workspace_id=workspace_id,
        )
        return audit_log
