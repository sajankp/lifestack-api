"""`ImportModule.finance_account_statement` row validation and commit adapter
(spec-078 — wallet ledger reconciliation / statement matching).

Generic CSV mapping in v1: date, description, debit, credit, balance. Date
parsing is user-specified at upload time (owner decision, spec-078) — the
mapping step includes a date-format identifier from `ALLOWED_DATE_FORMATS`,
applied uniformly to the whole file. There is no per-row auto-detection.

INV-1 (matching is metadata, never mutation): committing a statement writes
ONLY new `account_statements` / `statement_lines` rows. It never creates,
edits, or deletes `spending_transactions`, `capital_transfers`, or any
snapshot row.

INV-4 (idempotent re-import): `external_ref` is a deterministic hash of
(account, date, amount, normalized description, within-file duplicate
index) — re-uploading an overlapping statement produces the same refs for
the overlapping rows, so `commit_finance_account_statement_chunk` can skip
them as duplicates via the unique (account_id, external_ref) constraint.
"""

import hashlib
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.finance.models import Account, AccountType
from app.finance.statement_models import AccountStatement, StatementLine
from app.imports.models import ImportBatch, ImportPreviewRow
from app.imports.repository import ImportRepository
from app.imports.shared import AddErrorFn, norm

# Identifier -> strptime pattern. Small, fixed set (owner decision,
# spec-078) — no bank-specific fixtures, the user picks the format that
# matches their exported CSV.
ALLOWED_DATE_FORMATS: dict[str, str] = {
    "yyyy-MM-dd": "%Y-%m-%d",
    "dd/MM/yyyy": "%d/%m/%Y",
    "dd-MM-yyyy": "%d-%m-%Y",
    "MM/dd/yyyy": "%m/%d/%Y",
    "dd MMM yyyy": "%d %b %Y",
}


async def validate_finance_account_statement_upload(
    session: AsyncSession,
    workspace_id: int,
    filename: str,
    target_account_id: uuid.UUID | None,
    date_format: str | None,
) -> dict:
    if not (filename.lower().endswith(".csv") or filename.lower().endswith(".xlsx")):
        raise ValidationError(detail="Only .csv and .xlsx files are supported")
    if target_account_id is None:
        raise ValidationError(detail="target_account_id is required for statement imports")
    if date_format not in ALLOWED_DATE_FORMATS:
        raise ValidationError(detail=f"date_format must be one of {sorted(ALLOWED_DATE_FORMATS)}")
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
    if account.account_type == AccountType.brokerage:
        raise ValidationError(
            detail="Statement reconciliation is for ledger-managed (wallet/bank/card) "
            "accounts; brokerage cash is reconciled against snapshots instead."
        )
    return {
        "target_account_id": str(target_account_id),
        "date_format": date_format,
        "account_currency": account.default_currency_code,
    }


def _normalize_description(description: str) -> str:
    return " ".join(description.split()).lower()


def validate_finance_account_statement_row(
    row: dict,
    add_error: AddErrorFn,
    *,
    date_format: str,
    dup_counts: dict[str, int],
) -> tuple[dict, None]:
    date_raw = norm(row.get("date"))
    description_raw = norm(row.get("description"))
    debit_raw = norm(row.get("debit"))
    credit_raw = norm(row.get("credit"))
    balance_raw = norm(row.get("balance"))

    occurred_at: date | None = None
    if date_raw:
        try:
            occurred_at = datetime.strptime(date_raw, ALLOWED_DATE_FORMATS[date_format]).date()
        except Exception:
            add_error(
                "date",
                "invalid_date",
                f"date does not match the selected format {date_format}",
                date_raw,
            )
    else:
        add_error("date", "required", "date is required", date_raw)

    if not description_raw:
        add_error("description", "required", "description is required", description_raw)

    debit = credit = None
    if debit_raw:
        try:
            debit = Decimal(debit_raw)
            if debit <= 0:
                raise InvalidOperation
        except Exception:
            add_error("debit", "invalid_decimal", "debit must be a positive decimal", debit_raw)
    if credit_raw:
        try:
            credit = Decimal(credit_raw)
            if credit <= 0:
                raise InvalidOperation
        except Exception:
            add_error("credit", "invalid_decimal", "credit must be a positive decimal", credit_raw)

    amount = None
    if debit_raw and credit_raw:
        add_error(
            "debit",
            "both_debit_and_credit",
            "exactly one of debit/credit must be set, not both",
            debit_raw,
        )
    elif debit is not None:
        amount = -debit
    elif credit is not None:
        amount = credit
    elif not debit_raw and not credit_raw:
        add_error("debit", "required", "one of debit/credit is required", None)

    balance = None
    if balance_raw:
        try:
            balance = Decimal(balance_raw)
        except Exception:
            add_error("balance", "invalid_decimal", "balance must be a decimal", balance_raw)

    external_ref = None
    if occurred_at is not None and amount is not None and description_raw:
        dedup_key = f"{occurred_at.isoformat()}|{amount}|{_normalize_description(description_raw)}"
        dup_index = dup_counts.get(dedup_key, 0)
        dup_counts[dedup_key] = dup_index + 1
        external_ref = hashlib.sha256(f"{dedup_key}|{dup_index}".encode()).hexdigest()[:32]

    payload = {
        "occurred_at": occurred_at.isoformat() if occurred_at else None,
        "description": description_raw,
        "amount": str(amount) if amount is not None else None,
        "balance": str(balance) if balance is not None else None,
        "external_ref": external_ref,
    }
    return payload, None


async def prepare_finance_account_statement_commit(
    session: AsyncSession,
    repository: ImportRepository,
    workspace_id: int,
    batch: ImportBatch,
) -> AccountStatement:
    """Pre-step run once before the chunked commit loop: create (or reuse,
    on re-commit-after-failure) the `AccountStatement` header row, deriving
    period/closing-balance from the full set of validated preview rows.
    """
    extra = batch.extra_json or {}
    account_public_id = uuid.UUID(extra["target_account_id"])
    account = (
        await session.execute(
            select(Account).where(
                Account.workspace_id == workspace_id, Account.public_id == account_public_id
            )
        )
    ).scalar_one()

    preview_rows = await repository.iter_preview_rows(batch.id)
    dates: list[date] = []
    last_balance: Decimal | None = None
    last_date: date | None = None
    for row in preview_rows:
        p = row.payload_json
        d = date.fromisoformat(p["occurred_at"])
        dates.append(d)
        if p.get("balance") is not None and (last_date is None or d >= last_date):
            last_balance = Decimal(p["balance"])
            last_date = d

    existing = (
        await session.execute(
            select(AccountStatement).where(AccountStatement.import_batch_id == batch.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    statement = AccountStatement(
        workspace_id=workspace_id,
        account_id=account.id,
        period_start=min(dates),
        period_end=max(dates),
        closing_balance=last_balance,
        currency_code=account.default_currency_code,
        import_batch_id=batch.id,
    )
    session.add(statement)
    await session.flush()
    return statement


async def commit_finance_account_statement_chunk(
    session: AsyncSession,
    workspace_id: int,
    account_id: int,
    statement: AccountStatement,
    rows: list[ImportPreviewRow],
) -> tuple[int, dict]:
    # Idempotency (INV-4) via a pre-check, not insert-then-catch: a caught
    # IntegrityError would force a session-wide rollback, discarding every
    # other row (and the AccountStatement header) already flushed earlier in
    # this same commit transaction.
    chunk_refs = [r.payload_json["external_ref"] for r in rows]
    existing_refs = set(
        (
            await session.execute(
                select(StatementLine.external_ref).where(
                    StatementLine.account_id == account_id,
                    StatementLine.external_ref.in_(chunk_refs),
                )
            )
        )
        .scalars()
        .all()
    )

    inserted = 0
    skipped = 0
    for r in rows:
        p = r.payload_json
        if p["external_ref"] in existing_refs:
            skipped += 1
            continue
        line = StatementLine(
            workspace_id=workspace_id,
            account_id=account_id,
            statement_id=statement.id,
            occurred_at=date.fromisoformat(p["occurred_at"]),
            description=p["description"],
            amount=Decimal(p["amount"]),
            balance=Decimal(p["balance"]) if p.get("balance") is not None else None,
            external_ref=p["external_ref"],
        )
        session.add(line)
        existing_refs.add(p["external_ref"])
        inserted += 1

    await session.flush()
    return inserted, {"skipped": skipped}
