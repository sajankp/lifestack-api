import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.dependencies import (
    get_current_user,
    get_current_workspace_id,
    get_spending_budget_service,
    get_spending_category_service,
    get_spending_transaction_service,
)
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
    CategoryUpdate,
    TransactionCreate,
    TransactionResponse,
    TransactionUpdate,
)
from app.spending.service import BudgetService, CategoryService, TransactionService

router = APIRouter(prefix="/spending", tags=["spending"])


# ---------------------------------------------------------------------------
# Response helpers — map internal category_id (int) → public_id (UUID)
# ---------------------------------------------------------------------------


def _category_response(cat: SpendingCategory) -> CategoryResponse:
    return CategoryResponse.model_validate(cat)


def _transaction_response(
    tx: SpendingTransaction, category_public_id: uuid.UUID
) -> TransactionResponse:
    data = tx.model_dump()
    data["category_id"] = category_public_id
    return TransactionResponse.model_validate(data)


def _budget_response(budget: SpendingBudget, category_public_id: uuid.UUID) -> BudgetResponse:
    data = budget.model_dump()
    data["category_id"] = category_public_id
    return BudgetResponse.model_validate(data)


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


@router.get("/categories", response_model=list[CategoryResponse])
async def list_categories(
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    cats = await category_service.list_categories(workspace_id)
    return [_category_response(c) for c in cats]


@router.post("/categories", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category(
    category_in: CategoryCreate,
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    cat = await category_service.create_category(workspace_id, category_in)
    return _category_response(cat)


@router.patch("/categories/{category_id}", response_model=CategoryResponse)
async def update_category(
    category_id: uuid.UUID,
    category_in: CategoryUpdate,
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    cat = await category_service.update_category(workspace_id, category_id, category_in)
    return _category_response(cat)


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: uuid.UUID,
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    await category_service.delete_category(workspace_id, category_id)


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


@router.get("/transactions", response_model=list[TransactionResponse])
async def list_transactions(
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    category_id: uuid.UUID | None = Query(None),
    type: TransactionType | None = Query(None),
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
):
    txs = await transaction_service.list_transactions(
        workspace_id,
        category_public_id=category_id,
        type_filter=type,
        from_date=from_date,
        to_date=to_date,
    )
    # Resolve category public_ids for response
    result = []
    category_cache: dict[int, uuid.UUID] = {}
    for tx in txs:
        if tx.category_id not in category_cache:
            cat = await category_service.list_categories(workspace_id)
            category_cache = {c.id: c.public_id for c in cat}  # type: ignore[union-attr]
        result.append(_transaction_response(tx, category_cache[tx.category_id]))
    return result


@router.post(
    "/transactions", response_model=TransactionResponse, status_code=status.HTTP_201_CREATED
)
async def create_transaction(
    tx_in: TransactionCreate,
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    tx = await transaction_service.create_transaction(user["id"], workspace_id, tx_in)
    cat = await category_service.get_category(workspace_id, tx_in.category_id)
    return _transaction_response(tx, cat.public_id)


@router.get("/transactions/{transaction_id}", response_model=TransactionResponse)
async def get_transaction(
    transaction_id: uuid.UUID,
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    tx = await transaction_service.get_transaction(workspace_id, transaction_id)
    cats = await category_service.list_categories(workspace_id)
    cat_map = {c.id: c.public_id for c in cats}  # type: ignore[union-attr]
    return _transaction_response(tx, cat_map[tx.category_id])


@router.patch("/transactions/{transaction_id}", response_model=TransactionResponse)
async def update_transaction(
    transaction_id: uuid.UUID,
    tx_in: TransactionUpdate,
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    tx = await transaction_service.update_transaction(workspace_id, transaction_id, tx_in)
    cats = await category_service.list_categories(workspace_id)
    cat_map = {c.id: c.public_id for c in cats}  # type: ignore[union-attr]
    return _transaction_response(tx, cat_map[tx.category_id])


@router.delete("/transactions/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_transaction(
    transaction_id: uuid.UUID,
    transaction_service: Annotated[TransactionService, Depends(get_spending_transaction_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    await transaction_service.delete_transaction(workspace_id, transaction_id)


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


@router.get("/budgets", response_model=list[BudgetResponse])
async def list_budgets(
    budget_service: Annotated[BudgetService, Depends(get_spending_budget_service)],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    budgets = await budget_service.list_budgets(workspace_id)
    cats = await category_service.list_categories(workspace_id)
    cat_map = {c.id: c.public_id for c in cats}  # type: ignore[union-attr]
    return [_budget_response(b, cat_map[b.category_id]) for b in budgets]


@router.post("/budgets", response_model=BudgetResponse, status_code=status.HTTP_201_CREATED)
async def create_budget(
    budget_in: BudgetCreate,
    budget_service: Annotated[BudgetService, Depends(get_spending_budget_service)],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    budget = await budget_service.create_budget(workspace_id, budget_in)
    cat = await category_service.get_category(workspace_id, budget_in.category_id)
    return _budget_response(budget, cat.public_id)


@router.patch("/budgets/{budget_id}", response_model=BudgetResponse)
async def update_budget(
    budget_id: uuid.UUID,
    budget_in: BudgetUpdate,
    budget_service: Annotated[BudgetService, Depends(get_spending_budget_service)],
    category_service: Annotated[CategoryService, Depends(get_spending_category_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    budget = await budget_service.update_budget(workspace_id, budget_id, budget_in)
    cats = await category_service.list_categories(workspace_id)
    cat_map = {c.id: c.public_id for c in cats}  # type: ignore[union-attr]
    return _budget_response(budget, cat_map[budget.category_id])
