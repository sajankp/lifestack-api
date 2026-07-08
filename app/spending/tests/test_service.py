"""Unit tests for spending services — repositories are mocked."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import APIError
from app.finance.models import Account, AccountType
from app.spending.models import (
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
    TransactionType,
)
from app.spending.schemas import (
    BudgetCreate,
    BudgetUpdate,
    CategoryCreate,
    TransactionCreate,
    TransactionUpdate,
)
from app.spending.service import BudgetService, CategoryService, TransactionService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_category(
    workspace_id: int = 1,
    name: str = "Food & Dining",
    is_system: bool = True,
    id: int = 10,
) -> SpendingCategory:
    return SpendingCategory(
        id=id,
        public_id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=name,
        normalized_name=name.strip().lower(),
        is_system=is_system,
    )


def _make_transaction(workspace_id: int = 1, category_id: int = 10) -> SpendingTransaction:
    return SpendingTransaction(
        id=1,
        public_id=uuid.uuid4(),
        workspace_id=workspace_id,
        user_id=1,
        category_id=category_id,
        amount=Decimal("99.99"),
        type=TransactionType.expense,
        occurred_at=datetime.now(UTC),
    )


def _make_budget(workspace_id: int = 1, category_id: int = 10) -> SpendingBudget:
    return SpendingBudget(
        id=1,
        public_id=uuid.uuid4(),
        workspace_id=workspace_id,
        category_id=category_id,
        amount=Decimal("500.00"),
        start_month=date(2026, 3, 1),
        end_month=None,
    )


# ---------------------------------------------------------------------------
# CategoryService tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_cat_repo():
    return AsyncMock()


@pytest.fixture
def cat_service(mock_cat_repo):
    return CategoryService(repository=mock_cat_repo)


@pytest.mark.asyncio
async def test_list_categories(cat_service, mock_cat_repo):
    mock_cat_repo.get_all.return_value = ([], 0)
    result = await cat_service.list_categories(workspace_id=1)
    mock_cat_repo.get_all.assert_called_once_with(1, 50, 0)
    assert result == ([], 0)


@pytest.mark.asyncio
async def test_get_category_not_found(cat_service, mock_cat_repo):
    mock_cat_repo.get_by_public_id.return_value = None
    with pytest.raises(APIError) as exc:
        await cat_service.get_category(workspace_id=1, public_id=uuid.uuid4())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_create_category_success(cat_service, mock_cat_repo):
    mock_cat_repo.get_by_normalized_name.return_value = None
    cat = _make_category(is_system=False)
    mock_cat_repo.create.return_value = cat

    result = await cat_service.create_category(1, CategoryCreate(name="Utilities"))
    assert mock_cat_repo.create.called
    assert result == cat


@pytest.mark.asyncio
async def test_create_category_duplicate_name_rejected(cat_service, mock_cat_repo):
    mock_cat_repo.get_by_normalized_name.return_value = _make_category()
    with pytest.raises(APIError) as exc:
        await cat_service.create_category(1, CategoryCreate(name="Food & Dining"))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_delete_unused_system_category_succeeds(cat_service, mock_cat_repo):
    system_cat = _make_category(is_system=True)
    mock_cat_repo.get_by_public_id.return_value = system_cat
    mock_cat_repo.has_usage.return_value = False
    await cat_service.delete_category(workspace_id=1, public_id=system_cat.public_id)
    mock_cat_repo.delete.assert_called_once_with(system_cat)


@pytest.mark.asyncio
async def test_delete_category_in_use_rejected(cat_service, mock_cat_repo):
    custom_cat = _make_category(is_system=False)
    mock_cat_repo.get_by_public_id.return_value = custom_cat
    mock_cat_repo.has_usage.return_value = True
    with pytest.raises(APIError) as exc:
        await cat_service.delete_category(workspace_id=1, public_id=custom_cat.public_id)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_delete_in_use_system_category_rejected(cat_service, mock_cat_repo):
    system_cat = _make_category(is_system=True)
    mock_cat_repo.get_by_public_id.return_value = system_cat
    mock_cat_repo.has_usage.return_value = True
    with pytest.raises(APIError) as exc:
        await cat_service.delete_category(workspace_id=1, public_id=system_cat.public_id)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_delete_custom_category_success(cat_service, mock_cat_repo):
    custom_cat = _make_category(is_system=False)
    mock_cat_repo.get_by_public_id.return_value = custom_cat
    mock_cat_repo.has_usage.return_value = False
    await cat_service.delete_category(workspace_id=1, public_id=custom_cat.public_id)
    mock_cat_repo.delete.assert_called_once_with(custom_cat)


@pytest.mark.asyncio
async def test_provision_default_categories(cat_service, mock_cat_repo):
    await cat_service.provision_default_categories(workspace_id=5)
    mock_cat_repo.create_many.assert_called_once()
    categories_arg = mock_cat_repo.create_many.call_args[0][0]
    assert all(c.workspace_id == 5 for c in categories_arg)
    assert all(c.is_system for c in categories_arg)
    assert len(categories_arg) == 8  # 8 default categories


# ---------------------------------------------------------------------------
# TransactionService tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_tx_repo():
    return AsyncMock()


@pytest.fixture
def mock_cat_repo_for_tx():
    return AsyncMock()


@pytest.fixture
def mock_account_repo_for_tx():
    return AsyncMock()


@pytest.fixture
def tx_service(mock_tx_repo, mock_cat_repo_for_tx, mock_account_repo_for_tx):
    return TransactionService(mock_tx_repo, mock_cat_repo_for_tx, mock_account_repo_for_tx)


@pytest.mark.asyncio
async def test_create_transaction_cross_workspace_category_rejected(
    tx_service, mock_cat_repo_for_tx
):
    """Category not in this workspace → 404 (cross-workspace ref rejected)."""
    mock_cat_repo_for_tx.get_by_public_id.return_value = None
    with pytest.raises(APIError) as exc:
        await tx_service.create_transaction(
            user_id=1,
            workspace_id=1,
            tx_in=TransactionCreate(
                category_id=uuid.uuid4(),
                amount=Decimal("50.00"),
                type=TransactionType.expense,
                occurred_at=datetime.now(UTC),
            ),
        )
    assert exc.value.status_code == 404
    assert "Cross-workspace" in exc.value.detail


@pytest.mark.asyncio
async def test_create_transaction_success(
    tx_service, mock_tx_repo, mock_cat_repo_for_tx, mock_account_repo_for_tx
):
    cat = _make_category()
    mock_cat_repo_for_tx.get_by_public_id.return_value = cat
    account = Account(
        id=20,
        public_id=uuid.uuid4(),
        workspace_id=1,
        name="Wallet",
        account_type=AccountType.wallet,
        default_currency_code="USD",
    )
    mock_account_repo_for_tx.get_by_public_id.return_value = account
    tx = _make_transaction()
    mock_tx_repo.create.return_value = tx

    result = await tx_service.create_transaction(
        user_id=1,
        workspace_id=1,
        tx_in=TransactionCreate(
            category_id=cat.public_id,
            account_id=account.public_id,
            amount=Decimal("99.99"),
            type=TransactionType.expense,
            occurred_at=datetime.now(UTC),
        ),
    )
    assert result == tx
    mock_tx_repo.create.assert_called_once()


@pytest.mark.asyncio
async def test_get_transaction_not_found(tx_service, mock_tx_repo):
    mock_tx_repo.get_by_public_id.return_value = None
    with pytest.raises(APIError) as exc:
        await tx_service.get_transaction(workspace_id=1, public_id=uuid.uuid4())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_update_transaction_cross_workspace_category_rejected(
    tx_service, mock_tx_repo, mock_cat_repo_for_tx
):
    """Cross-workspace category substitution is blocked on update too."""
    tx = _make_transaction()
    mock_tx_repo.get_by_public_id.return_value = tx
    mock_cat_repo_for_tx.get_by_public_id.return_value = None  # category not in this workspace

    with pytest.raises(APIError) as exc:
        await tx_service.update_transaction(
            workspace_id=1,
            public_id=tx.public_id,
            tx_in=TransactionUpdate(category_id=uuid.uuid4()),
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# BudgetService tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_budget_repo():
    return AsyncMock()


@pytest.fixture
def mock_cat_repo_for_budget():
    return AsyncMock()


@pytest.fixture
def budget_service(mock_budget_repo, mock_cat_repo_for_budget):
    return BudgetService(mock_budget_repo, mock_cat_repo_for_budget)


@pytest.mark.asyncio
async def test_create_budget_success(budget_service, mock_budget_repo, mock_cat_repo_for_budget):
    cat = _make_category()
    mock_cat_repo_for_budget.get_by_public_id.return_value = cat
    mock_budget_repo.get_overlapping_budgets.return_value = []
    budget = _make_budget()
    mock_budget_repo.create.return_value = budget

    result = await budget_service.create_budget(
        workspace_id=1,
        budget_in=BudgetCreate(
            category_id=cat.public_id,
            amount=Decimal("500.00"),
            start_month=date(2026, 3, 1),
        ),
    )
    assert result == budget
    mock_budget_repo.create.assert_called_once()


@pytest.mark.asyncio
async def test_create_budget_duplicate_rejected(
    budget_service, mock_budget_repo, mock_cat_repo_for_budget
):
    cat = _make_category()
    mock_cat_repo_for_budget.get_by_public_id.return_value = cat
    mock_budget_repo.get_overlapping_budgets.return_value = [_make_budget()]  # already exists

    with pytest.raises(APIError) as exc:
        await budget_service.create_budget(
            workspace_id=1,
            budget_in=BudgetCreate(
                category_id=cat.public_id,
                amount=Decimal("500.00"),
                start_month=date(2026, 3, 1),
            ),
        )
    assert exc.value.status_code == 409
    assert "PATCH" in exc.value.detail  # hints at how to update


@pytest.mark.asyncio
async def test_create_budget_cross_workspace_category_rejected(
    budget_service, mock_cat_repo_for_budget
):
    mock_cat_repo_for_budget.get_by_public_id.return_value = None
    with pytest.raises(APIError) as exc:
        await budget_service.create_budget(
            workspace_id=1,
            budget_in=BudgetCreate(
                category_id=uuid.uuid4(),
                amount=Decimal("100.00"),
                start_month=date(2026, 3, 1),
            ),
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_update_budget_success(budget_service, mock_budget_repo, mock_cat_repo_for_budget):
    budget = _make_budget()
    mock_budget_repo.get_by_public_id.return_value = budget
    mock_budget_repo.get_overlapping_budgets.return_value = []
    mock_budget_repo.save.return_value = budget

    result = await budget_service.update_budget(
        workspace_id=1,
        public_id=budget.public_id,
        budget_in=BudgetUpdate(amount=Decimal("750.00")),
    )
    assert result.amount == Decimal("750.00")
    mock_budget_repo.save.assert_called_once()


@pytest.mark.asyncio
async def test_get_budget_not_found(budget_service, mock_budget_repo):
    mock_budget_repo.get_by_public_id.return_value = None
    with pytest.raises(APIError) as exc:
        await budget_service.get_budget(workspace_id=1, public_id=uuid.uuid4())
    assert exc.value.status_code == 404
