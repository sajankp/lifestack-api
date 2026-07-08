"""`ImportModule.spending_transactions` and `ImportModule.spending_budgets`
row validation, upload-time checks, and commit.

Grouped in one file (rather than two) because both are small, plain CSV
imports into the spending module with no cross-chunk state beyond the
category-name cache the shared harness already resolves once per commit.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.finance.models import Account, WorkspaceFinanceSetting
from app.imports.models import ImportBatch, ImportPreviewRow
from app.imports.shared import AddErrorFn, WeightEntry, norm
from app.spending.models import (
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
    TransactionSourceType,
    TransactionType,
)

SPENDING_TRANSACTIONS_TEMPLATE_ROW = "2026-05-01T09:30:00Z,expense,42.50,Food & Dining,Breakfast"
SPENDING_BUDGETS_TEMPLATE_ROW = "2026-05-01,Food & Dining,800.00"


async def validate_spending_transactions_upload(
    session: AsyncSession,
    workspace_id: int,
    filename: str,
    target_account_id: uuid.UUID | None,
) -> dict | None:
    """Validate an upload destined for a spending-transactions import and
    return the `extra_json` payload (or `None`) to store on the new
    `ImportBatch`.
    """
    if not (filename.lower().endswith(".csv") or filename.lower().endswith(".xlsx")):
        raise ValidationError(detail="Only .csv and .xlsx files are supported")
    # Optional import-level fallback account (spec-054) — used for rows whose
    # account_name is missing/unmatched, before falling back to the
    # workspace default.
    if target_account_id is None:
        return None
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
    return {"target_account_id": str(target_account_id)}


async def resolve_spending_transactions_fallback_account_id(
    session: AsyncSession, workspace_id: int, batch: ImportBatch
) -> int | None:
    """Resolve the fallback account (spec-054) for rows with no matched
    `account_name`: the import-level target account set at upload time, else
    the workspace default. Resolved once per batch, not per row.
    """
    target_account_public_id = (batch.extra_json or {}).get("target_account_id")
    fallback_account_id: int | None = None
    if target_account_public_id:
        target_account = (
            await session.execute(
                select(Account).where(
                    Account.workspace_id == workspace_id,
                    Account.public_id == uuid.UUID(target_account_public_id),
                    Account.is_active,
                )
            )
        ).scalar_one_or_none()
        fallback_account_id = target_account.id if target_account else None
    if fallback_account_id is None:
        finance_setting = (
            await session.execute(
                select(WorkspaceFinanceSetting).where(
                    WorkspaceFinanceSetting.workspace_id == workspace_id
                )
            )
        ).scalar_one_or_none()
        if finance_setting and finance_setting.default_spending_account_id is not None:
            default_account = (
                await session.execute(
                    select(Account).where(
                        Account.workspace_id == workspace_id,
                        Account.id == finance_setting.default_spending_account_id,
                        Account.is_active,
                    )
                )
            ).scalar_one_or_none()
            fallback_account_id = default_account.id if default_account else None
    return fallback_account_id


def validate_spending_transaction_row(
    row: dict,
    add_error: AddErrorFn,
    *,
    header_mode: str,
    by_name: dict[str, int],
    by_public: dict[str, int],
    account_map: dict[str, int],
    fallback_account_id: int | None,
) -> tuple[dict, WeightEntry | None]:
    if header_mode == "spendee":
        occurred_raw = norm(row.get("Date"))
        raw_type = norm(row.get("Type"))
        type_raw = raw_type.lower() if raw_type else ""
        amount_raw = norm(row.get("Amount"))
        category_raw = norm(row.get("Category name"))
        description_raw = norm(row.get("Note")) or None
        account_name_raw = norm(row.get("Wallet")) or None
        labels_raw = norm(row.get("Labels")) or None
    else:
        occurred_raw = norm(row.get("occurred_at"))
        raw_type = norm(row.get("type"))
        type_raw = raw_type.lower() if raw_type else ""
        amount_raw = norm(row.get("amount"))
        category_raw = norm(row.get("category"))
        description_raw = norm(row.get("description")) or None
        account_name_raw = norm(row.get("account_name")) or None
        labels_raw = None

    try:
        occurred_at = datetime.fromisoformat(occurred_raw.replace("Z", "+00:00"))
    except Exception:
        add_error(
            "occurred_at",
            "invalid_datetime",
            "occurred_at must be ISO datetime/date",
            occurred_raw,
        )
        occurred_at = None

    if type_raw not in {"income", "expense"}:
        add_error("type", "invalid_enum", "type must be income or expense", type_raw)

    try:
        amount = Decimal(amount_raw)
        if header_mode == "spendee" and type_raw == "expense" and amount < 0:
            amount = abs(amount)
        if header_mode == "spendee" and type_raw == "income" and amount < 0:
            add_error(
                "amount",
                "invalid_decimal",
                "income rows cannot have negative amount",
                amount_raw,
            )
            amount = None
        if amount is not None and amount <= 0:
            raise InvalidOperation
    except Exception:
        add_error(
            "amount",
            "invalid_decimal",
            "amount must be a positive decimal",
            amount_raw,
        )
        amount = None

    category_id = None
    if category_raw:
        category_id = by_public.get(category_raw) or by_name.get(category_raw.lower())
    else:
        add_error("category", "required", "category is required", category_raw)

    # Resolution order (spec-054): row's account_name match → import-level
    # target account → workspace default → row-level error (blocks commit
    # for that row only).
    account_id = None
    if account_name_raw:
        account_id = account_map.get(account_name_raw.lower())
        if account_id is None:
            add_error(
                "account_name",
                "not_found",
                "account not found in workspace",
                account_name_raw,
            )
    else:
        account_id = fallback_account_id
        if account_id is None:
            add_error(
                "account_name",
                "required",
                "account_name is required — set a target account for this "
                "import or a default spending account in Finance Settings",
                account_name_raw,
            )

    payload = {
        "occurred_at": occurred_at.isoformat() if occurred_at else None,
        "type": type_raw,
        "amount": str(amount) if amount is not None else None,
        "category_id": category_id,
        "category_name": category_raw if category_raw else None,
        "description": description_raw,
        "account_name": account_name_raw,
        "account_id": account_id,
        "labels": labels_raw,
    }
    return payload, None


def validate_spending_budget_row(
    row: dict,
    add_error: AddErrorFn,
    *,
    by_name: dict[str, int],
    by_public: dict[str, int],
) -> tuple[dict, WeightEntry | None]:
    month_raw = norm(row.get("month_start"))
    category_raw = norm(row.get("category"))
    amount_raw = norm(row.get("amount"))

    try:
        month_start = datetime.fromisoformat(month_raw).date()
        if month_start.day != 1:
            raise ValueError
    except Exception:
        add_error(
            "month_start",
            "invalid_month",
            "month_start must be YYYY-MM-01",
            month_raw,
        )
        month_start = None

    category_id = by_public.get(category_raw) or by_name.get(category_raw.lower())
    if category_id is None:
        add_error("category", "not_found", "category not found in workspace", category_raw)

    try:
        amount = Decimal(amount_raw)
        if amount <= 0:
            raise InvalidOperation
    except Exception:
        add_error(
            "amount",
            "invalid_decimal",
            "amount must be a positive decimal",
            amount_raw,
        )
        amount = None

    payload = {
        "month_start": month_start.isoformat() if month_start else None,
        "category_id": category_id,
        "category_name": category_raw if category_raw else None,
        "amount": str(amount) if amount is not None else None,
    }
    return payload, None


async def commit_spending_transactions_chunk(
    session: AsyncSession,
    workspace_id: int,
    user_id: int,
    batch: ImportBatch,
    rows: list[ImportPreviewRow],
    by_name: dict[str, int],
    auto_created_categories: list[str],
) -> int:
    inserted = 0
    for row in rows:
        p = row.payload_json
        category_id = p.get("category_id")
        if category_id is None:
            category_name_raw = norm(p.get("category_name"))
            if not category_name_raw:
                raise ValidationError(detail="category is required")
            category_name = category_name_raw.lower()
            if category_name in by_name:
                category_id = by_name[category_name]
            else:
                category = SpendingCategory(
                    workspace_id=workspace_id,
                    name=category_name_raw,
                    normalized_name=category_name,
                    is_system=False,
                )
                session.add(category)
                await session.flush()
                category_id = category.id
                if category_id is None:
                    raise ValidationError(detail="failed to create category")
                by_name[category_name] = category_id
                auto_created_categories.append(category.name)
        tx = SpendingTransaction(
            workspace_id=workspace_id,
            user_id=user_id,
            category_id=int(category_id),
            amount=Decimal(p["amount"]),
            type=TransactionType(p["type"]),
            occurred_at=datetime.fromisoformat(p["occurred_at"]),
            description=p.get("description"),
            account_id=p.get("account_id"),
            labels=p.get("labels"),
            source_type=TransactionSourceType.imported,
            source_import_id=batch.id,
            source_ref=f"{batch.public_id}:{row.row_number}",
        )
        session.add(tx)
        inserted += 1
    return inserted


async def commit_spending_budgets_chunk(
    session: AsyncSession,
    workspace_id: int,
    batch: ImportBatch,
    rows: list[ImportPreviewRow],
) -> int:
    budget_keys = {
        (
            int(row.payload_json["category_id"]),
            datetime.fromisoformat(row.payload_json["month_start"]).date(),
        )
        for row in rows
    }
    existing_budgets = {}
    if budget_keys:
        budget_rows = (
            (
                await session.execute(
                    select(SpendingBudget).where(
                        SpendingBudget.workspace_id == workspace_id,
                        tuple_(SpendingBudget.category_id, SpendingBudget.start_month).in_(
                            budget_keys
                        ),
                        SpendingBudget.end_month == SpendingBudget.start_month,
                    )
                )
            )
            .scalars()
            .all()
        )
        existing_budgets = {
            (budget.category_id, budget.start_month): budget for budget in budget_rows
        }

    inserted = 0
    for row in rows:
        p = row.payload_json
        month_start_date = datetime.fromisoformat(p["month_start"]).date()
        budget_key = (int(p["category_id"]), month_start_date)
        existing_budget = existing_budgets.get(budget_key)

        if existing_budget:
            existing_budget.amount = Decimal(p["amount"])
            existing_budget.source_type = "imported"
            existing_budget.source_import_id = batch.id
            existing_budget.source_ref = f"{batch.public_id}:{row.row_number}"
            existing_budget.updated_at = datetime.now(UTC)
        else:
            budget = SpendingBudget(
                workspace_id=workspace_id,
                category_id=int(p["category_id"]),
                amount=Decimal(p["amount"]),
                start_month=month_start_date,
                end_month=month_start_date,
                source_type="imported",
                source_import_id=batch.id,
                source_ref=f"{batch.public_id}:{row.row_number}",
            )
            session.add(budget)
            existing_budgets[budget_key] = budget
        inserted += 1
    return inserted
