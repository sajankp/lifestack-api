import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.audit import AuditLogger
from app.core.dependencies import (
    get_audit_logger,
    get_current_user,
    get_current_workspace_id,
    get_finance_account_service,
    get_spending_budget_service,
    get_spending_category_service,
    get_spending_recurring_service,
    get_spending_transaction_service,
)
from app.core.pagination import PaginatedResponse, PaginationParams
from app.finance.service import AccountService
from app.spending.models import (
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
    TransactionType,
)
from app.spending.schemas import (
    BudgetCreate,
    BudgetResponse,
    BudgetUpdate,
    CategoryCreate,
    CategoryResponse,
    CategorySpendTotal,
    CategoryUpdate,
    RecurringTransactionCreate,
    RecurringTransactionResponse,
    RecurringTransactionUpdate,
    SpendingTrendResponse,
    TransactionCreate,
    TransactionResponse,
    TransactionSummaryResponse,
    TransactionUpdate,
    UpcomingPreviewResponse,
)
from app.spending.service import (
    BudgetService,
    CategoryService,
    RecurringTransactionService,
    TransactionService,
)

router = APIRouter(prefix="/spending", tags=["spending"])


# ---------------------------------------------------------------------------
# Response helpers — map internal category_id (int) → public_id (UUID)
# ---------------------------------------------------------------------------


def _category_response(cat: SpendingCategory) -> CategoryResponse:
    return CategoryResponse.model_validate(cat)


def _transaction_response(
    tx: SpendingTransaction,
    category_public_id: uuid.UUID,
    account_public_id: uuid.UUID | None = None,
) -> TransactionResponse:
    data = tx.model_dump()
    data["category_id"] = category_public_id
    data["account_id"] = account_public_id
    return TransactionResponse.model_validate(data)


def _budget_response(budget: SpendingBudget, category_public_id: uuid.UUID) -> BudgetResponse:
    data = budget.model_dump()
    data["category_id"] = category_public_id
    return BudgetResponse.model_validate(data)


def _recurring_response(recurring, category_public_id: uuid.UUID) -> RecurringTransactionResponse:
    data = recurring.model_dump()
    data["category_id"] = category_public_id
    return RecurringTransactionResponse.model_validate(data)


async def _build_category_cache(
    category_service: CategoryService, workspace_id: int
) -> dict[int, uuid.UUID]:
    """Fetch all categories once and build an int-id → public_id lookup."""
    cats, _ = await category_service.list_categories(workspace_id, limit=10000, offset=0)
    return {c.id: c.public_id for c in cats}  # type: ignore[union-attr]


async def _build_account_cache(
    account_service: AccountService, workspace_id: int
) -> dict[int, uuid.UUID]:
    accounts, _ = await account_service.list_accounts(workspace_id, limit=10000, offset=0)
    return {a.id: a.public_id for a in accounts}  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


@router.get("/categories", response_model=PaginatedResponse[CategoryResponse])
async def list_categories(
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
):
    cats, total = await category_service.list_categories(
        workspace_id, pagination.limit, pagination.offset
    )
    return PaginatedResponse(
        items=[_category_response(c) for c in cats],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post("/categories", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category(
    category_in: CategoryCreate,
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
):
    cat = await category_service.create_category(
        workspace_id, category_in, actor_id=user["id"], audit_logger=audit_logger
    )
    return _category_response(cat)


@router.get("/categories/{category_id}", response_model=CategoryResponse)
async def get_category(
    category_id: uuid.UUID,
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    cat = await category_service.get_category(workspace_id, category_id)
    return _category_response(cat)


@router.patch("/categories/{category_id}", response_model=CategoryResponse)
async def update_category(
    category_id: uuid.UUID,
    category_in: CategoryUpdate,
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
):
    cat = await category_service.update_category(
        workspace_id,
        category_id,
        category_in,
        actor_id=user["id"],
        audit_logger=audit_logger,
    )
    return _category_response(cat)


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: uuid.UUID,
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
):
    await category_service.delete_category(
        workspace_id, category_id, actor_id=user["id"], audit_logger=audit_logger
    )


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


@router.get("/transactions", response_model=PaginatedResponse[TransactionResponse])
async def list_transactions(
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    category_id: uuid.UUID | None = Query(None),
    type: TransactionType | None = Query(None),
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
):
    txs, total = await transaction_service.list_transactions(
        workspace_id,
        category_public_id=category_id,
        type_filter=type,
        from_date=from_date,
        to_date=to_date,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    # Build category cache once before the loop
    cat_cache = await _build_category_cache(category_service, workspace_id)
    account_cache = await _build_account_cache(account_service, workspace_id)
    return PaginatedResponse(
        items=[
            _transaction_response(
                tx,
                cat_cache[tx.category_id],
                account_cache.get(tx.account_id) if tx.account_id is not None else None,
            )
            for tx in txs
        ],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get("/transactions/summary", response_model=TransactionSummaryResponse)
async def get_transaction_summary(
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    category_id: uuid.UUID | None = Query(None),
    from_date: datetime = Query(...),
    to_date: datetime = Query(...),
):
    income_total = await transaction_service.get_sum_by_type(
        workspace_id=workspace_id,
        type_filter=TransactionType.income,
        from_date=from_date,
        to_date=to_date,
        category_public_id=category_id,
    )
    if category_id is not None:
        expense_total = await transaction_service.get_sum_by_type(
            workspace_id=workspace_id,
            type_filter=TransactionType.expense,
            from_date=from_date,
            to_date=to_date,
            category_public_id=category_id,
        )
        category_totals = [CategorySpendTotal(category_id=category_id, total=expense_total)]
    else:
        raw_totals = await transaction_service.get_category_totals(
            workspace_id=workspace_id,
            from_date=from_date,
            to_date=to_date,
            type_filter=TransactionType.expense,
        )
        expense_total = sum(raw_totals.values())
        cat_cache = await _build_category_cache(category_service, workspace_id)
        category_totals = [
            CategorySpendTotal(category_id=cat_cache[cat_id], total=total)
            for cat_id, total in raw_totals.items()
        ]

    return TransactionSummaryResponse(
        income_total=income_total,
        expense_total=expense_total,
        net_total=income_total - expense_total,
        category_totals=category_totals,
    )


@router.get("/analytics/trends", response_model=SpendingTrendResponse)
async def get_spending_trends(
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    from_month: date = Query(..., alias="from"),
    to_month: date = Query(..., alias="to"),
):
    start = from_month.replace(day=1)
    end = to_month.replace(day=1)
    return await transaction_service.get_monthly_trends(workspace_id, start, end)


@router.post(
    "/transactions", response_model=TransactionResponse, status_code=status.HTTP_201_CREATED
)
async def create_transaction(
    tx_in: TransactionCreate,
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
):
    tx = await transaction_service.create_transaction(
        user["id"], workspace_id, tx_in, audit_logger=audit_logger
    )
    cat = await category_service.get_category(workspace_id, tx_in.category_id)
    account_public_id = tx_in.account_id if tx.account_id is not None else None
    return _transaction_response(tx, cat.public_id, account_public_id)


@router.get("/transactions/{transaction_id}", response_model=TransactionResponse)
async def get_transaction(
    transaction_id: uuid.UUID,
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    tx = await transaction_service.get_transaction(workspace_id, transaction_id)
    cat_cache = await _build_category_cache(category_service, workspace_id)
    account_cache = await _build_account_cache(account_service, workspace_id)
    return _transaction_response(
        tx,
        cat_cache[tx.category_id],
        account_cache.get(tx.account_id) if tx.account_id is not None else None,
    )


@router.patch("/transactions/{transaction_id}", response_model=TransactionResponse)
async def update_transaction(
    transaction_id: uuid.UUID,
    tx_in: TransactionUpdate,
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    account_service: Annotated[AccountService, Depends(get_finance_account_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
):
    tx = await transaction_service.update_transaction(
        workspace_id,
        transaction_id,
        tx_in,
        actor_id=user["id"],
        audit_logger=audit_logger,
    )
    cat_cache = await _build_category_cache(category_service, workspace_id)
    account_cache = await _build_account_cache(account_service, workspace_id)
    return _transaction_response(
        tx,
        cat_cache[tx.category_id],
        account_cache.get(tx.account_id) if tx.account_id is not None else None,
    )


@router.delete("/transactions/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_transaction(
    transaction_id: uuid.UUID,
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
):
    await transaction_service.delete_transaction(
        workspace_id, transaction_id, actor_id=user["id"], audit_logger=audit_logger
    )


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


@router.get("/budgets", response_model=PaginatedResponse[BudgetResponse])
async def list_budgets(
    budget_service: Annotated[BudgetService, Depends(get_spending_budget_service)],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    month_start: date | None = Query(None),
):
    budgets, total = await budget_service.list_budgets(
        workspace_id, pagination.limit, pagination.offset, month_start=month_start
    )
    cat_cache = await _build_category_cache(category_service, workspace_id)
    return PaginatedResponse(
        items=[_budget_response(b, cat_cache[b.category_id]) for b in budgets],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post("/budgets", response_model=BudgetResponse, status_code=status.HTTP_201_CREATED)
async def create_budget(
    budget_in: BudgetCreate,
    budget_service: Annotated[BudgetService, Depends(get_spending_budget_service)],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
):
    budget = await budget_service.create_budget(
        workspace_id, budget_in, actor_id=user["id"], audit_logger=audit_logger
    )
    cat = await category_service.get_category(workspace_id, budget_in.category_id)
    return _budget_response(budget, cat.public_id)


@router.patch("/budgets/{budget_id}", response_model=BudgetResponse)
async def update_budget(
    budget_id: uuid.UUID,
    budget_in: BudgetUpdate,
    budget_service: Annotated[BudgetService, Depends(get_spending_budget_service)],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
):
    budget = await budget_service.update_budget(
        workspace_id,
        budget_id,
        budget_in,
        actor_id=user["id"],
        audit_logger=audit_logger,
    )
    cat_cache = await _build_category_cache(category_service, workspace_id)
    return _budget_response(budget, cat_cache[budget.category_id])


@router.get("/recurring", response_model=PaginatedResponse[RecurringTransactionResponse])
async def list_recurring(
    recurring_service: Annotated[
        RecurringTransactionService, Depends(get_spending_recurring_service)
    ],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    is_active: bool | None = Query(True),
):
    items, total = await recurring_service.list_recurring(
        workspace_id, is_active, pagination.limit, pagination.offset
    )
    cat_cache = await _build_category_cache(category_service, workspace_id)
    return PaginatedResponse(
        items=[_recurring_response(item, cat_cache[item.category_id]) for item in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post(
    "/recurring", response_model=RecurringTransactionResponse, status_code=status.HTTP_201_CREATED
)
async def create_recurring(
    payload: RecurringTransactionCreate,
    recurring_service: Annotated[
        RecurringTransactionService, Depends(get_spending_recurring_service)
    ],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    item = await recurring_service.create_recurring(workspace_id, user["id"], payload)
    return _recurring_response(item, payload.category_id)


@router.get("/recurring/upcoming", response_model=UpcomingPreviewResponse)
async def get_upcoming_preview(
    recurring_service: Annotated[
        RecurringTransactionService, Depends(get_spending_recurring_service)
    ],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    days: int = Query(default=30, ge=1, le=365),
):
    """Return a read-only projection of upcoming recurring transactions (no DB writes)."""
    return await recurring_service.upcoming_preview(
        workspace_id, days, recurring_service.category_repo
    )


@router.get("/recurring/{recurring_id}", response_model=RecurringTransactionResponse)
async def get_recurring(
    recurring_id: uuid.UUID,
    recurring_service: Annotated[
        RecurringTransactionService, Depends(get_spending_recurring_service)
    ],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    item = await recurring_service.get_recurring(workspace_id, recurring_id)
    cat_cache = await _build_category_cache(category_service, workspace_id)
    return _recurring_response(item, cat_cache[item.category_id])


@router.patch("/recurring/{recurring_id}", response_model=RecurringTransactionResponse)
async def patch_recurring(
    recurring_id: uuid.UUID,
    payload: RecurringTransactionUpdate,
    recurring_service: Annotated[
        RecurringTransactionService, Depends(get_spending_recurring_service)
    ],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    item = await recurring_service.update_recurring(workspace_id, recurring_id, payload)
    cat_cache = await _build_category_cache(category_service, workspace_id)
    return _recurring_response(item, cat_cache[item.category_id])


@router.delete("/recurring/{recurring_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_recurring(
    recurring_id: uuid.UUID,
    recurring_service: Annotated[
        RecurringTransactionService, Depends(get_spending_recurring_service)
    ],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    await recurring_service.deactivate_recurring(workspace_id, recurring_id)
