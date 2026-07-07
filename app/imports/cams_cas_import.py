"""CAMS CAS (Consolidated Account Statement) import: upload validation and
the PDF-parse-driven "validate" step for `ImportModule.investing_cams_cas`.

Committing a CAMS CAS batch reuses the investing-orders committer in
`app/imports/investing_orders_import.py` — both modules feed the same
`InvestingOrderCreate` pipeline, so there is nothing module-specific to
extract on the commit side.
"""

import asyncio
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditLogger
from app.core.exceptions import NotFoundError, ValidationError
from app.finance.models import Account
from app.imports.cams_cas_parser import parse_cams_cas
from app.imports.models import ImportBatch, ImportPreviewRow, ImportStatus
from app.imports.repository import ImportRepository
from app.imports.shared import enum_value
from app.investing.models import CorporateAction


async def validate_cams_cas_upload(
    session: AsyncSession,
    workspace_id: int,
    filename: str,
    target_account_id: uuid.UUID | None,
) -> dict:
    """Validate an upload destined for a CAMS CAS import and return the
    `extra_json` payload to store on the new `ImportBatch`."""
    if not filename.lower().endswith(".pdf"):
        raise ValidationError(detail="Only .pdf files are supported for CAMS CAS imports")
    if target_account_id is None:
        raise ValidationError(detail="target_account_id is required for CAMS CAS imports")
    account = (
        await session.execute(
            select(Account).where(
                Account.workspace_id == workspace_id,
                Account.public_id == target_account_id,
                Account.is_active,
            )
        )
    ).scalar_one_or_none()
    if account is None:
        raise NotFoundError(
            detail=f"Account with id {target_account_id} not found in this workspace"
        )
    if account.account_type != "brokerage":
        raise ValidationError(
            detail=(
                f"CAMS CAS imports can only target brokerage accounts. "
                f"Account '{account.name}' is type '{account.account_type}'"
            )
        )
    return {"target_account_id": str(target_account_id)}


async def _filter_recorded_corporate_actions(
    session: AsyncSession, workspace_id: int, target_account_id: uuid.UUID, suspected: list[dict]
) -> list[dict]:
    """Drop discontinuity warnings already covered by a recorded CorporateAction (spec-051)."""
    if not suspected:
        return suspected
    account = (
        await session.execute(
            select(Account).where(
                Account.workspace_id == workspace_id,
                Account.public_id == target_account_id,
            )
        )
    ).scalar_one_or_none()
    if account is None or account.id is None:
        return suspected

    symbols = {entry["symbol"].upper() for entry in suspected}
    actions = (
        (
            await session.execute(
                select(CorporateAction).where(
                    CorporateAction.workspace_id == workspace_id,
                    CorporateAction.account_id == account.id,
                    CorporateAction.symbol.in_(symbols),
                )
            )
        )
        .scalars()
        .all()
    )
    actions_by_symbol: dict[str, list[CorporateAction]] = {}
    for action in actions:
        actions_by_symbol.setdefault(action.symbol, []).append(action)

    filtered: list[dict] = []
    for entry in suspected:
        from_date = date.fromisoformat(entry["from_date"])
        to_date = date.fromisoformat(entry["to_date"])
        already_recorded = any(
            from_date <= action.ex_date <= to_date
            for action in actions_by_symbol.get(entry["symbol"].upper(), [])
        )
        if not already_recorded:
            filtered.append(entry)
    return filtered


async def validate_cams_cas_batch(
    session: AsyncSession,
    repository: ImportRepository,
    workspace_id: int,
    user_id: int,
    batch: ImportBatch,
    file_path: str,
    audit_logger: AuditLogger,
) -> tuple[ImportBatch, list]:
    target_account_id = (batch.extra_json or {}).get("target_account_id")
    if not target_account_id:
        raise ValidationError(detail="target_account_id is required for CAMS CAS imports")

    try:
        parse_result = await asyncio.to_thread(parse_cams_cas, file_path)
    except Exception as exc:
        raise ValidationError(
            detail=(
                "Failed to parse CAMS CAS PDF. If the file is password-protected, "
                "remove the password before uploading."
            )
        ) from exc

    previews: list[ImportPreviewRow] = []
    for row_no, order in enumerate(parse_result.orders, start=1):
        payload = {
            "order_type": order["order_type"],
            "symbol": order["symbol"],
            "instrument_type": order["instrument_type"],
            "instrument_name": order["instrument_name"],
            "account_name": None,
            "account_public_id": target_account_id,
            "quantity": order["quantity"],
            "price_per_unit": order["price_per_unit"],
            "currency": order["currency"],
            "brokerage_fee": "0",
            "tax_amount": "0",
            "other_fees": "0",
            "occurred_at": order["occurred_at"],
            "exchange_name": None,
            "notes": order["notes"],
        }
        previews.append(
            ImportPreviewRow(import_batch_id=batch.id, row_number=row_no, payload_json=payload)
        )
    if previews:
        await repository.add_preview_rows(previews)

    corporate_action_suspected = await _filter_recorded_corporate_actions(
        session, workspace_id, uuid.UUID(target_account_id), parse_result.corporate_action_suspected
    )

    batch.extra_json = {
        **(batch.extra_json or {}),
        "skipped": parse_result.skipped,
        "corporate_action_suspected": corporate_action_suspected,
    }
    batch.total_rows = len(parse_result.orders) + len(parse_result.skipped)
    batch.valid_rows = len(parse_result.orders)
    batch.error_rows = 0
    batch.validated_at = datetime.now(UTC)
    batch.status = ImportStatus.validated
    batch.updated_at = datetime.now(UTC)
    batch = await repository.save_batch(batch)

    await audit_logger.log(
        workspace_id=workspace_id,
        actor_id=user_id,
        action="import_validated",
        module="import",
        entity_type="import_batch",
        entity_id=batch.id,  # type: ignore[arg-type]
        details={
            "entity_public_id": str(batch.public_id),
            "before": None,
            "after": {
                "module": enum_value(batch.module),
                "status": enum_value(batch.status),
                "total_rows": batch.total_rows,
                "valid_rows": batch.valid_rows,
                "error_rows": batch.error_rows,
            },
            "changed_fields": ["module", "status", "total_rows", "valid_rows", "error_rows"],
        },
    )

    return batch, []
