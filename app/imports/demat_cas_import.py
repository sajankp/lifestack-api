"""Demat CAS (NSDL/CDSL Consolidated Account Statement) import for
`ImportModule.investing_demat_cas`: upload validation, the PDF-parse-driven
"validate" step, and the commit-side accumulation/finalization that builds
one `HoldingVerification` row per commit (spec-060 NSDL, spec-063 CDSL).
Registrar (NSDL vs CDSL) is auto-detected from the PDF text by
`demat_cas_parser.parse_demat_cas` and carried through `extra_json["source"]`
to the persisted `HoldingVerification.source`.
"""

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from pdfminer.pdfdocument import PDFPasswordIncorrect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditLogger
from app.core.exceptions import NotFoundError, ValidationError
from app.finance.models import Account
from app.imports.demat_cas_parser import UnrecognizedRegistrarError, parse_demat_cas
from app.imports.models import ImportBatch, ImportPreviewRow, ImportStatus
from app.imports.repository import ImportRepository
from app.imports.shared import enum_value
from app.investing.models import CorporateAction, Holding, HoldingVerification


async def validate_demat_cas_upload(
    session: AsyncSession,
    workspace_id: int,
    filename: str,
    target_account_id: uuid.UUID | None,
) -> dict:
    """Validate an upload destined for a Demat CAS import and return the
    `extra_json` payload to store on the new `ImportBatch`."""
    if not filename.lower().endswith(".pdf"):
        raise ValidationError(detail="Only .pdf files are supported for Demat CAS imports")
    if target_account_id is None:
        raise ValidationError(detail="target_account_id is required for Demat CAS imports")
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
                f"Demat CAS imports can only target brokerage accounts. "
                f"Account '{account.name}' is type '{account.account_type}'"
            )
        )
    return {"target_account_id": str(target_account_id)}


def _is_plausible_split_ratio(ratio: Decimal) -> bool:
    """A drift ratio that looks like a forward or reverse split (spec-060).

    Heuristic, not proof: within 1% of an integer >= 2 (forward split,
    e.g. 10:1), or within 1% of the reciprocal of an integer >= 2
    (reverse split, e.g. 1:10). Real depository/broker quantities are
    exact, so an un-applied split shows up as an exact ratio in practice;
    the tolerance only guards against Decimal rounding noise.
    """
    if ratio <= 0:
        return False
    nearest = ratio.to_integral_value()
    if nearest >= 2 and abs(ratio - nearest) <= Decimal("0.01") * nearest:
        return True
    if ratio < 1:
        inverse = Decimal("1") / ratio
        inverse_nearest = inverse.to_integral_value()
        if (
            inverse_nearest >= 2
            and abs(inverse - inverse_nearest) <= Decimal("0.01") * inverse_nearest
        ):
            return True
    return False


async def validate_demat_cas_batch(
    session: AsyncSession,
    repository: ImportRepository,
    workspace_id: int,
    user_id: int,
    batch: ImportBatch,
    file_path: str,
    audit_logger: AuditLogger,
    file_password: str | None,
) -> tuple[ImportBatch, list]:
    target_account_id = (batch.extra_json or {}).get("target_account_id")
    if not target_account_id:
        raise ValidationError(detail="target_account_id is required for Demat CAS imports")

    account = (
        await session.execute(
            select(Account).where(
                Account.workspace_id == workspace_id,
                Account.public_id == uuid.UUID(target_account_id),
            )
        )
    ).scalar_one_or_none()
    if account is None or account.id is None:
        raise NotFoundError(
            detail=f"Account with id {target_account_id} not found in this workspace"
        )

    try:
        parse_result, source = await asyncio.to_thread(parse_demat_cas, file_path, file_password)
    except UnrecognizedRegistrarError as exc:
        # Neither/both NSDL and CDSL markers found — a clean format error,
        # never a silent mis-route to the wrong registrar's parser (spec-063).
        raise ValidationError(
            detail="Failed to parse Demat CAS PDF: could not identify it as an NSDL "
            "or CDSL statement. The file may be corrupted or in an unsupported format."
        ) from exc
    except Exception as exc:
        # pdfplumber wraps every PDFDocument-construction failure in its own
        # PdfminerException, with the real underlying error as args[0] — so a
        # wrong password (PDFPasswordIncorrect) needs an explicit isinstance
        # check to distinguish from a genuinely corrupted/unexpected-format
        # file. Blaming the password for every failure would mislead a user
        # whose PDF is just broken.
        underlying = exc.args[0] if exc.args else None
        if isinstance(underlying, PDFPasswordIncorrect):
            raise ValidationError(
                detail=(
                    "Failed to parse Demat CAS PDF: incorrect password. Check that "
                    "the password matches the PAN-derived password printed on your "
                    "NSDL/CDSL statement."
                )
            ) from exc
        raise ValidationError(
            detail="Failed to parse Demat CAS PDF. The file may be corrupted or in "
            "an unexpected format."
        ) from exc

    # Symbols are ISINs for Indian instruments (spec-056's symbol choice),
    # so a Holding's symbol lines up with a depository ISIN directly.
    holding_rows = (
        (
            await session.execute(
                select(Holding).where(
                    Holding.workspace_id == workspace_id,
                    Holding.account_id == account.id,
                )
            )
        )
        .scalars()
        .all()
    )
    holdings_by_isin = {h.symbol.upper(): h for h in holding_rows}
    depository_by_isin = {h["isin"]: h for h in parse_result.holdings}

    suspected_isins: set[str] = set()
    report: list[dict] = []
    for isin in sorted(set(holdings_by_isin) | set(depository_by_isin)):
        holding = holdings_by_isin.get(isin)
        depository = depository_by_isin.get(isin)
        lifestack_qty = holding.quantity if holding is not None else None
        depository_qty = Decimal(depository["quantity"]) if depository is not None else None

        if holding is not None and depository is not None:
            if lifestack_qty == depository_qty:
                status_value, delta = "match", Decimal("0")
            else:
                status_value = "quantity_drift"
                delta = depository_qty - lifestack_qty
                if lifestack_qty > 0 and _is_plausible_split_ratio(depository_qty / lifestack_qty):
                    suspected_isins.add(isin)
        elif depository is not None:
            status_value, delta = "missing_in_lifestack", None
        else:
            status_value, delta = "missing_at_depository", None

        report.append({
            "isin": isin,
            "security_name": depository["security_name"] if depository else None,
            "depository_quantity": str(depository_qty) if depository_qty is not None else None,
            "lifestack_quantity": str(lifestack_qty) if lifestack_qty is not None else None,
            "status": status_value,
            "delta": str(delta) if delta is not None else None,
            "corporate_action_suspected": isin in suspected_isins,
        })

    # Suppress the hint for ISINs that already have a recorded corporate
    # action on this account (same intent as spec-056's price-discontinuity
    # filter — a simpler existence check since we have no NAV time series
    # here to bound an ex-date window).
    if suspected_isins:
        recorded_rows = (
            (
                await session.execute(
                    select(CorporateAction.symbol).where(
                        CorporateAction.workspace_id == workspace_id,
                        CorporateAction.account_id == account.id,
                        CorporateAction.symbol.in_(suspected_isins),
                    )
                )
            )
            .scalars()
            .all()
        )
        recorded_symbols = {s.upper() for s in recorded_rows}
        if recorded_symbols:
            for entry in report:
                if entry["isin"] in recorded_symbols:
                    entry["corporate_action_suspected"] = False

    previews: list[ImportPreviewRow] = []
    for row_no, entry in enumerate(report, start=1):
        previews.append(
            ImportPreviewRow(import_batch_id=batch.id, row_number=row_no, payload_json=entry)
        )
    if previews:
        await repository.add_preview_rows(previews)

    batch.extra_json = {
        **(batch.extra_json or {}),
        "skipped": parse_result.skipped,
        "statement_date": parse_result.statement_date,
        "source": source,
    }
    batch.total_rows = len(report)
    batch.valid_rows = len(report)
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


async def finalize_demat_cas_commit(
    session: AsyncSession,
    workspace_id: int,
    batch: ImportBatch,
    demat_cas_report: list[dict],
) -> int:
    """Build the one `HoldingVerification` row for a committed Demat CAS batch.

    Returns the new `inserted` count (always 1) — this overwrites, rather
    than adds to, whatever `inserted` was accumulated during the chunk loop
    (which is always 0 for this module, since rows are only appended to
    `demat_cas_report`, never counted), matching the original code exactly.
    """
    target_account_id = (batch.extra_json or {}).get("target_account_id")
    account = (
        await session.execute(
            select(Account).where(
                Account.workspace_id == workspace_id,
                Account.public_id == uuid.UUID(target_account_id),
            )
        )
    ).scalar_one_or_none()
    if account is None or account.id is None:
        raise ValidationError(detail="Target account no longer exists")

    statement_date_str = (batch.extra_json or {}).get("statement_date")
    counts = {
        "match": 0,
        "quantity_drift": 0,
        "missing_in_lifestack": 0,
        "missing_at_depository": 0,
    }
    for entry in demat_cas_report:
        counts[entry["status"]] += 1

    verification = HoldingVerification(
        workspace_id=workspace_id,
        account_id=account.id,
        source_import_id=batch.id,
        source=(batch.extra_json or {}).get("source", "nsdl_cas"),
        statement_date=date.fromisoformat(statement_date_str) if statement_date_str else None,
        match_count=counts["match"],
        quantity_drift_count=counts["quantity_drift"],
        missing_in_lifestack_count=counts["missing_in_lifestack"],
        missing_at_depository_count=counts["missing_at_depository"],
        report_json=demat_cas_report,
    )
    session.add(verification)
    await session.flush()
    return 1
