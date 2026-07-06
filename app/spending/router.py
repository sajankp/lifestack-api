import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.audit import AuditLogger
from app.core.dependencies import (
    get_audit_logger,
    get_current_user,
    get_current_workspace_id,
    get_spending_budget_service,
    get_spending_category_service,
    get_spending_recurring_service,
    get_spending_transaction_service,
    require_min_role,
)
from app.core.pagination import PaginatedResponse, PaginationParams, build_page
from app.spending.models import (
    TransactionSort,
    TransactionType,
)
from app.spending.response_helpers import (
    category_response,
)
from app.spending.schemas import (
    BudgetCreate,
    BudgetPerformanceResponse,
    BudgetResponse,
    BudgetUpdate,
    CategoryBreakdownResponse,
    CategoryCreate,
    CategoryResponse,
    CategorySpendTotal,
    CategoryUpdate,
    LedgerResponse,
    RecurringTransactionCreate,
    RecurringTransactionResponse,
    RecurringTransactionUpdate,
    SavingsRateResponse,
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
    return build_page([category_response(c) for c in cats], total, pagination)


@router.post("/categories", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category(
    category_in: CategoryCreate,
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    cat = await category_service.create_category(
        workspace_id, category_in, actor_id=user["id"], audit_logger=audit_logger
    )
    return category_response(cat)


@router.get("/categories/{category_id}", response_model=CategoryResponse)
async def get_category(
    category_id: uuid.UUID,
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    cat = await category_service.get_category(workspace_id, category_id)
    return category_response(cat)


@router.patch("/categories/{category_id}", response_model=CategoryResponse)
async def update_category(
    category_id: uuid.UUID,
    category_in: CategoryUpdate,
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    cat = await category_service.update_category(
        workspace_id,
        category_id,
        category_in,
        actor_id=user["id"],
        audit_logger=audit_logger,
    )
    return category_response(cat)


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: uuid.UUID,
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
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
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    category_id: uuid.UUID | None = Query(None),
    account_id: uuid.UUID | None = Query(None),
    unassigned: bool = Query(False, description="Return only transactions with no account set"),
    type: TransactionType | None = Query(None),
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
    sort: TransactionSort | None = Query(
        None,
        description=(
            "Sort order: date_desc/date_asc (by transaction date) or "
            "amount_desc/amount_asc. Defaults to newest-created first."
        ),
    ),
):
    detailed_items, total = await transaction_service.list_transactions_with_details(
        workspace_id,
        category_public_id=category_id,
        account_public_id=account_id,
        unassigned_only=unassigned,
        type_filter=type,
        from_date=from_date,
        to_date=to_date,
        sort=sort,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return build_page(detailed_items, total, pagination)


@router.get("/transactions/summary", response_model=TransactionSummaryResponse)
async def get_transaction_summary(
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    category_id: uuid.UUID | None = Query(None),
    account_id: uuid.UUID | None = Query(None),
    from_date: datetime = Query(...),
    to_date: datetime = Query(...),
):
    income_total = await transaction_service.get_sum_by_type(
        workspace_id=workspace_id,
        type_filter=TransactionType.income,
        from_date=from_date,
        to_date=to_date,
        category_public_id=category_id,
        account_public_id=account_id,
    )
    if category_id is not None:
        expense_total = await transaction_service.get_sum_by_type(
            workspace_id=workspace_id,
            type_filter=TransactionType.expense,
            from_date=from_date,
            to_date=to_date,
            category_public_id=category_id,
            account_public_id=account_id,
        )
        category_totals = [CategorySpendTotal(category_id=category_id, total=expense_total)]
    else:
        raw_totals = await transaction_service.get_category_totals(
            workspace_id=workspace_id,
            from_date=from_date,
            to_date=to_date,
            type_filter=TransactionType.expense,
            account_public_id=account_id,
        )
        expense_total = sum(raw_totals.values())
        cat_cache = await transaction_service._build_category_cache(workspace_id)
        category_totals = [
            CategorySpendTotal(category_id=cat_cache.get(cat_id), total=total)
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


@router.get("/analytics/breakdown", response_model=CategoryBreakdownResponse)
async def get_category_breakdown(
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
    type: TransactionType = Query(TransactionType.expense),
    limit: int = Query(default=10, ge=1, le=100),
):
    return await transaction_service.get_category_breakdown(
        workspace_id=workspace_id,
        from_date=from_date,
        to_date=to_date,
        type_filter=type,
        limit=limit,
    )


@router.get("/analytics/budget-performance", response_model=BudgetPerformanceResponse)
async def get_budget_performance(
    budget_service: Annotated[BudgetService, Depends(get_spending_budget_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    from_month: date = Query(..., alias="from"),
    to_month: date = Query(..., alias="to"),
):
    return await budget_service.get_budget_performance(
        workspace_id=workspace_id,
        from_month=from_month,
        to_month=to_month,
    )


@router.get("/analytics/savings-rate", response_model=SavingsRateResponse)
async def get_savings_rate(
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    from_month: date = Query(..., alias="from"),
    to_month: date = Query(..., alias="to"),
):
    return await transaction_service.get_savings_rate(
        workspace_id=workspace_id,
        from_month=from_month,
        to_month=to_month,
    )


@router.post(
    "/transactions", response_model=TransactionResponse, status_code=status.HTTP_201_CREATED
)
async def create_transaction(
    tx_in: TransactionCreate,
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    return await transaction_service.create_transaction_with_details(
        user["id"], workspace_id, tx_in, audit_logger=audit_logger
    )


@router.get("/transactions/{transaction_id}", response_model=TransactionResponse)
async def get_transaction(
    transaction_id: uuid.UUID,
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    return await transaction_service.get_transaction_with_details(workspace_id, transaction_id)


@router.patch("/transactions/{transaction_id}", response_model=TransactionResponse)
async def update_transaction(
    transaction_id: uuid.UUID,
    tx_in: TransactionUpdate,
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    return await transaction_service.update_transaction_with_details(
        workspace_id,
        transaction_id,
        tx_in,
        actor_id=user["id"],
        audit_logger=audit_logger,
    )


@router.delete("/transactions/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_transaction(
    transaction_id: uuid.UUID,
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
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
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    month_start: date | None = Query(None),
):
    detailed_items, total = await budget_service.list_budgets_with_details(
        workspace_id, pagination.limit, pagination.offset, month_start=month_start
    )
    return build_page(detailed_items, total, pagination)


@router.post("/budgets", response_model=BudgetResponse, status_code=status.HTTP_201_CREATED)
async def create_budget(
    budget_in: BudgetCreate,
    budget_service: Annotated[BudgetService, Depends(get_spending_budget_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    return await budget_service.create_budget_with_details(
        workspace_id, budget_in, actor_id=user["id"], audit_logger=audit_logger
    )


@router.patch("/budgets/{budget_id}", response_model=BudgetResponse)
async def update_budget(
    budget_id: uuid.UUID,
    budget_in: BudgetUpdate,
    budget_service: Annotated[BudgetService, Depends(get_spending_budget_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    return await budget_service.update_budget_with_details(
        workspace_id,
        budget_id,
        budget_in,
        actor_id=user["id"],
        audit_logger=audit_logger,
    )


@router.get("/recurring", response_model=PaginatedResponse[RecurringTransactionResponse])
async def list_recurring(
    recurring_service: Annotated[
        RecurringTransactionService, Depends(get_spending_recurring_service)
    ],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    is_active: bool | None = Query(True),
):
    detailed_items, total = await recurring_service.list_recurring_with_details(
        workspace_id, is_active, pagination.limit, pagination.offset
    )
    return build_page(detailed_items, total, pagination)


@router.post(
    "/recurring", response_model=RecurringTransactionResponse, status_code=status.HTTP_201_CREATED
)
async def create_recurring(
    payload: RecurringTransactionCreate,
    recurring_service: Annotated[
        RecurringTransactionService, Depends(get_spending_recurring_service)
    ],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    return await recurring_service.create_recurring_with_details(workspace_id, user["id"], payload)


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
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    return await recurring_service.get_recurring_with_details(workspace_id, recurring_id)


@router.patch("/recurring/{recurring_id}", response_model=RecurringTransactionResponse)
async def patch_recurring(
    recurring_id: uuid.UUID,
    payload: RecurringTransactionUpdate,
    recurring_service: Annotated[
        RecurringTransactionService, Depends(get_spending_recurring_service)
    ],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    return await recurring_service.update_recurring_with_details(
        workspace_id, recurring_id, payload
    )


@router.delete("/recurring/{recurring_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_recurring(
    recurring_id: uuid.UUID,
    recurring_service: Annotated[
        RecurringTransactionService, Depends(get_spending_recurring_service)
    ],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await recurring_service.deactivate_recurring(workspace_id, recurring_id)


# ---------------------------------------------------------------------------
# Spending Account Ledger
# ---------------------------------------------------------------------------


@router.get("/accounts/{account_id}/ledger", response_model=LedgerResponse)
async def get_account_ledger(
    account_id: uuid.UUID,
    tx_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    from_date: datetime | None = Query(default=None),
    to_date: datetime | None = Query(default=None),
):
    """Return a paginated transaction ledger for a spending account.

    Entries are ordered most-recent first. Each entry includes a `running_balance`
    representing the cumulative account balance (income minus expenses) after that
    transaction. Opening and closing balances for the page are also returned.
    """
    return await tx_service.get_ledger(
        workspace_id=workspace_id,
        account_public_id=account_id,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        offset=offset,
    )
