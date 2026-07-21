import re
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field, SQLModel

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Scrubbing (invariant #8 — error_message must never carry raw PII)
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_LONG_DIGITS_RE = re.compile(r"\d{8,}")
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{20,}\b")


def scrub_error_message(message: str) -> str:
    """Best-effort redaction of free-text exception messages before they land in
    the job_failures ledger or an alert email. Not a full PII scanner — just the
    shapes that actually show up in this codebase's exception strings (emails,
    account/card-like digit runs, API-key/token-like blobs)."""
    scrubbed = _EMAIL_RE.sub("[REDACTED_EMAIL]", message)
    scrubbed = _LONG_DIGITS_RE.sub("[REDACTED_NUMBER]", scrubbed)
    scrubbed = _LONG_TOKEN_RE.sub("[REDACTED_TOKEN]", scrubbed)
    return scrubbed


# ---------------------------------------------------------------------------
# Database Model
# ---------------------------------------------------------------------------


class JobFailure(SQLModel, table=True):
    __tablename__ = "job_failures"

    id: int | None = Field(default=None, primary_key=True)
    public_id: uuid.UUID = Field(default_factory=uuid.uuid4, index=True, unique=True)
    job_name: str = Field(max_length=100, index=True)
    workspace_id: int | None = Field(default=None, foreign_key="workspaces.id", index=True)

    error_type: str = Field(max_length=200)
    error_message: str = Field(sa_type=sa.Text())
    attempts: int

    first_failed_at: datetime = Field(sa_type=sa.DateTime(timezone=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=sa.DateTime(timezone=True), index=True
    )
    notified_at: datetime | None = Field(default=None, sa_type=sa.DateTime(timezone=True))
    resolved_at: datetime | None = Field(
        default=None, sa_type=sa.DateTime(timezone=True), index=True
    )


# ---------------------------------------------------------------------------
# Writer — best-effort, never raises into the calling job
# ---------------------------------------------------------------------------


async def record_job_failure(
    session: AsyncSession,
    *,
    job_name: str,
    workspace_id: int | None,
    exc: Exception,
    attempts: int,
    first_failed_at: datetime,
) -> None:
    """Write one job_failures row in its own short transaction on ``session`` --
    reuses the caller's connection (no new session/connection is opened, which
    would break the single-connection advisory-lock invariant), and never raises:
    a failed ledger insert is logged and swallowed, exactly like the existing
    _workspace_failed handling it complements."""
    try:
        async with session.begin():
            session.add(
                JobFailure(
                    job_name=job_name,
                    workspace_id=workspace_id,
                    error_type=type(exc).__name__,
                    error_message=scrub_error_message(str(exc)),
                    attempts=attempts,
                    first_failed_at=first_failed_at,
                )
            )
    except Exception:
        logger.warning(
            "job_failure_ledger_write_failed",
            job_name=job_name,
            workspace_id=workspace_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Auto-resolve — self-heal on a later success
# ---------------------------------------------------------------------------


async def resolve_job_failures(
    session: AsyncSession, *, job_name: str, workspace_id: int | None
) -> None:
    """Mark this (job_name, workspace_id) unit's still-open rows resolved. Cheap
    UPDATE on the success path; guarded so it can never fail the job it's called
    from. workspace_id is nullable and SQL equality never matches NULL, so the
    two cases are branched explicitly rather than using a single `== workspace_id`."""
    try:
        stmt = update(JobFailure).where(
            JobFailure.job_name == job_name, JobFailure.resolved_at.is_(None)
        )
        stmt = stmt.where(
            JobFailure.workspace_id.is_(None)
            if workspace_id is None
            else JobFailure.workspace_id == workspace_id
        )
        async with session.begin():
            await session.execute(stmt.values(resolved_at=datetime.now(UTC)))
    except Exception:
        logger.warning(
            "job_failure_auto_resolve_failed",
            job_name=job_name,
            workspace_id=workspace_id,
            exc_info=True,
        )
