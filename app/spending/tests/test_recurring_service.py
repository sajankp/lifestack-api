"""Unit tests for RecurringTransactionService.

The advance_due_date arithmetic itself (including the calendar-recurrence-
modes cases added by spec-053) is covered by app/tests/test_recurrence.py
now that it lives in app.core.recurrence — this file only re-tests the
basic daily/weekly/yearly/monthly cases still directly relevant here.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import NotFoundError, ValidationError
from app.core.recurrence import advance_due_date
from app.finance.models import Account, AccountType
from app.spending.models import RecurringTransaction, SpendingCategory, TransactionType
from app.spending.schemas import RecurringTransactionCreate
from app.spending.service import RecurringTransactionService


def test_advance_due_date_daily():
    assert advance_due_date(date(2026, 1, 1), "daily", 1) == date(2026, 1, 2)
    assert advance_due_date(date(2026, 1, 1), "daily", 5) == date(2026, 1, 6)


def test_advance_due_date_weekly():
    assert advance_due_date(date(2026, 1, 1), "weekly", 1) == date(2026, 1, 8)
    assert advance_due_date(date(2026, 1, 1), "weekly", 2) == date(2026, 1, 15)


def test_advance_due_date_yearly():
    assert advance_due_date(date(2026, 1, 1), "yearly", 1) == date(2027, 1, 1)
    # Leap year handling
    assert advance_due_date(date(2024, 2, 29), "yearly", 1) == date(2025, 2, 28)
    assert advance_due_date(date(2024, 2, 29), "yearly", 4) == date(2028, 2, 29)


def test_advance_due_date_monthly():
    assert advance_due_date(date(2026, 1, 15), "monthly", 1) == date(2026, 2, 15)
    assert advance_due_date(date(2026, 1, 31), "monthly", 1) == date(2026, 2, 28)
    assert advance_due_date(date(2026, 1, 31), "monthly", 3) == date(2026, 4, 30)


@pytest.fixture
def mock_recurring_repo():
    return AsyncMock()


@pytest.fixture
def mock_tx_repo():
    return AsyncMock()


@pytest.fixture
def mock_category_repo():
    return AsyncMock()


@pytest.fixture
def mock_account_repo():
    return AsyncMock()


@pytest.fixture
def mock_setting_repo():
    return AsyncMock()


@pytest.fixture
def recurring_service(
    mock_recurring_repo, mock_tx_repo, mock_category_repo, mock_account_repo, mock_setting_repo
):
    return RecurringTransactionService(
        recurring_repo=mock_recurring_repo,
        tx_repo=mock_tx_repo,
        category_repo=mock_category_repo,
        account_repo=mock_account_repo,
        setting_repo=mock_setting_repo,
    )


@pytest.mark.asyncio
async def test_create_recurring_success(
    recurring_service, mock_recurring_repo, mock_category_repo, mock_account_repo
):
    workspace_id = 1
    user_id = 10
    cat_public_id = uuid.uuid4()
    account_public_id = uuid.uuid4()

    mock_category_repo.get_by_public_id.return_value = SpendingCategory(
        id=5,
        public_id=cat_public_id,
        workspace_id=workspace_id,
        name="Utilities",
        normalized_name="utilities",
        is_system=False,
    )
    mock_account_repo.get_by_public_id.return_value = Account(
        id=9,
        public_id=account_public_id,
        workspace_id=workspace_id,
        name="Checking",
        account_type=AccountType.wallet,
        default_currency_code="USD",
        is_active=True,
    )

    payload = RecurringTransactionCreate(
        category_id=cat_public_id,
        account_id=account_public_id,
        amount=Decimal("150.00"),
        type=TransactionType.expense,
        frequency="monthly",
        interval=1,
        anchor_date=date(2026, 6, 1),
        end_date=date(2026, 12, 31),
        description="Electricity bill",
    )

    mock_recurring_repo.create.side_effect = lambda x: x

    result = await recurring_service.create_recurring(workspace_id, user_id, payload)

    assert result.workspace_id == workspace_id
    assert result.user_id == user_id
    assert result.category_id == 5
    assert result.account_id == 9
    assert result.amount == Decimal("150.00")
    assert result.type == TransactionType.expense
    assert result.frequency == "monthly"
    assert result.interval == 1
    assert result.anchor_date == date(2026, 6, 1)
    assert result.next_due_date == date(2026, 6, 1)
    assert result.end_date == date(2026, 12, 31)
    assert result.description == "Electricity bill"
    assert result.is_active is True


@pytest.mark.asyncio
async def test_create_recurring_invalid_end_date(recurring_service, mock_category_repo):
    workspace_id = 1
    user_id = 10
    cat_public_id = uuid.uuid4()

    mock_category_repo.get_by_public_id.return_value = SpendingCategory(
        id=5,
        public_id=cat_public_id,
        workspace_id=workspace_id,
        name="Utilities",
    )

    payload = RecurringTransactionCreate(
        category_id=cat_public_id,
        amount=Decimal("150.00"),
        type=TransactionType.expense,
        frequency="monthly",
        interval=1,
        anchor_date=date(2026, 6, 1),
        end_date=date(2026, 5, 31),  # before anchor_date
        description="Electricity bill",
    )

    with pytest.raises(ValidationError) as exc:
        await recurring_service.create_recurring(workspace_id, user_id, payload)
    assert "end_date cannot be before anchor_date" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_create_recurring_category_not_found(recurring_service, mock_category_repo):
    workspace_id = 1
    user_id = 10
    cat_public_id = uuid.uuid4()

    mock_category_repo.get_by_public_id.return_value = None

    payload = RecurringTransactionCreate(
        category_id=cat_public_id,
        amount=Decimal("150.00"),
        type=TransactionType.expense,
        frequency="monthly",
        interval=1,
        anchor_date=date(2026, 6, 1),
    )

    with pytest.raises(NotFoundError) as exc:
        await recurring_service.create_recurring(workspace_id, user_id, payload)
    assert "not found in this workspace" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_deactivate_recurring(recurring_service, mock_recurring_repo):
    workspace_id = 1
    public_id = uuid.uuid4()
    existing = RecurringTransaction(
        id=1,
        public_id=public_id,
        workspace_id=workspace_id,
        user_id=10,
        category_id=5,
        amount=Decimal("100.00"),
        type=TransactionType.expense,
        frequency="monthly",
        interval=1,
        anchor_date=date(2026, 1, 1),
        next_due_date=date(2026, 1, 1),
        is_active=True,
    )
    mock_recurring_repo.get_by_public_id.return_value = existing
    mock_recurring_repo.save.side_effect = lambda x: x

    await recurring_service.deactivate_recurring(workspace_id, public_id)

    assert existing.is_active is False
    assert existing.updated_at is not None
    mock_recurring_repo.save.assert_called_once_with(existing)


@pytest.mark.asyncio
async def test_upcoming_preview_success(
    recurring_service, mock_recurring_repo, mock_category_repo, mock_account_repo
):
    workspace_id = 1
    cat_id = 5
    cat_public_id = uuid.uuid4()
    rule_public_id = uuid.uuid4()

    # Stub Categories
    mock_category_repo.get_all.return_value = (
        [SpendingCategory(id=cat_id, public_id=cat_public_id, name="Rent")],
        1,
    )
    mock_account_repo.list_workspace_accounts.return_value = ([], 0)

    # Stub active recurring rules using UTC-derived date for consistency with service logic.
    today = datetime.now(UTC).date()
    existing = RecurringTransaction(
        id=1,
        public_id=rule_public_id,
        workspace_id=workspace_id,
        user_id=10,
        category_id=cat_id,
        amount=Decimal("1200.00"),
        type=TransactionType.expense,
        frequency="monthly",
        interval=1,
        anchor_date=today,
        next_due_date=today,
        is_active=True,
    )
    mock_recurring_repo.get_all.return_value = ([existing], 1)

    result = await recurring_service.upcoming_preview(
        workspace_id, days=45, category_repo=mock_category_repo
    )

    # Within 45 days, we expect 2 occurrences: today and today + 1 month.
    assert len(result.items) >= 2
    assert result.items[0].recurring_public_id == rule_public_id
    assert result.items[0].category_id == cat_public_id
    assert result.items[0].amount == Decimal("1200.00")
    assert result.items[0].projected_date == today
    assert result.items[1].projected_date > today
