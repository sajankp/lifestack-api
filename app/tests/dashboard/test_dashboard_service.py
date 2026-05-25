from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.dashboard.schemas import DashboardSummary
from app.dashboard.service import DashboardService


@pytest.fixture
def mock_todo_service():
    return AsyncMock()


@pytest.fixture
def mock_transaction_service():
    return AsyncMock()


@pytest.fixture
def mock_budget_service():
    return AsyncMock()


@pytest.fixture
def mock_investing_service():
    return AsyncMock()


@pytest.fixture
def service(
    mock_todo_service, mock_transaction_service, mock_budget_service, mock_investing_service
):
    return DashboardService(
        todo_service=mock_todo_service,
        transaction_service=mock_transaction_service,
        budget_service=mock_budget_service,
        investing_summary_service=mock_investing_service,
    )


@pytest.mark.asyncio
async def test_get_summary_success(
    service,
    mock_todo_service,
    mock_transaction_service,
    mock_budget_service,
    mock_investing_service,
):
    workspace_id = 1

    # 1. Setup Todos mock returns
    mock_todo = MagicMock()
    mock_todo.public_id = "abc-123"
    mock_todo.title = "Review Budget"
    mock_todo.due_date = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    mock_todo.priority = "high"

    mock_todo_service.get_summary_counts.return_value = (10, 2)
    mock_todo_service.get_next_due_items.return_value = [mock_todo]
    mock_todo_service.get_active_guardrail_todo_count.return_value = 3

    # 2. Setup Spending mock returns
    mock_transaction_service.get_sum_by_type.return_value = Decimal("450.50")
    mock_budget_service.get_month_total_budget.return_value = Decimal("1000.00")

    # Category totals for top overspent categories calculation
    mock_transaction_service.get_category_totals.return_value = {
        101: Decimal("120.00"),  # budget 100 -> overspend 20
        102: Decimal("300.00"),  # budget 200 -> overspend 100
        103: Decimal("50.00"),  # budget 100 -> overspend 0 (no overspend)
        104: Decimal("90.00"),  # no budget -> skipped
    }

    budget1 = MagicMock(category_id=101, amount=Decimal("100.00"))
    budget2 = MagicMock(category_id=102, amount=Decimal("200.00"))
    budget3 = MagicMock(category_id=103, amount=Decimal("100.00"))

    mock_budget_service.list_budgets.return_value = ([budget1, budget2, budget3], 3)

    # 3. Setup Investing mock returns
    mock_investing_summary = MagicMock()
    mock_investing_summary.portfolio_value = Decimal("50000.00")
    mock_investing_summary.daily_change = Decimal("-150.25")
    mock_investing_summary.holdings_count = 8
    mock_investing_service.get_summary.return_value = mock_investing_summary

    # Run service method
    summary = await service.get_summary(workspace_id)

    # Asserts
    assert isinstance(summary, DashboardSummary)

    # Todos Summary
    assert summary.todos.status == "available"
    assert summary.todos.open_count == 10
    assert summary.todos.overdue_count == 2
    assert summary.todos.active_guardrail_todo_count == 3
    assert len(summary.todos.next_due_items) == 1
    assert summary.todos.next_due_items[0]["title"] == "Review Budget"

    # Spending Summary
    assert summary.spending.status == "available"
    assert summary.spending.month_spent == Decimal("450.50")
    assert summary.spending.month_budget == Decimal("1000.00")

    # Check top overspent categories sorting and filtration
    # Category 102 overspend = 100, ratio = 1.5
    # Category 101 overspend = 20, ratio = 1.2
    # Others have no overspend or no budget
    assert len(summary.spending.top_overspent_categories) == 2
    assert summary.spending.top_overspent_categories[0]["category_id"] == 102
    assert summary.spending.top_overspent_categories[0]["overspend"] == Decimal("100.00")
    assert summary.spending.top_overspent_categories[1]["category_id"] == 101
    assert summary.spending.top_overspent_categories[1]["overspend"] == Decimal("20.00")

    # Investing Summary
    assert summary.investing.status == "available"
    assert summary.investing.portfolio_value == Decimal("50000.00")
    assert summary.investing.daily_change == Decimal("-150.25")
    assert summary.investing.holdings_count == 8


@pytest.mark.asyncio
async def test_get_summary_graceful_failures(
    service,
    mock_todo_service,
    mock_transaction_service,
    mock_budget_service,
    mock_investing_service,
):
    workspace_id = 1

    # Case A: Todo service fails, others succeed
    mock_todo_service.get_summary_counts.side_effect = RuntimeError("Todo Service Failed")
    mock_transaction_service.get_sum_by_type.return_value = Decimal("10.0")
    mock_budget_service.get_month_total_budget.return_value = Decimal("20.0")
    mock_transaction_service.get_category_totals.return_value = {}
    mock_budget_service.list_budgets.return_value = ([], 0)
    mock_investing_service.get_summary.return_value = MagicMock(
        portfolio_value=Decimal("1.0"), daily_change=Decimal("0.0"), holdings_count=1
    )

    summary = await service.get_summary(workspace_id)
    assert summary.todos.status == "unavailable"
    assert summary.spending.status == "available"
    assert summary.investing.status == "available"

    # Case B: Spending service fails, others succeed
    mock_todo_service.get_summary_counts.side_effect = None
    mock_todo_service.get_summary_counts.return_value = (0, 0)
    mock_todo_service.get_next_due_items.return_value = []
    mock_todo_service.get_active_guardrail_todo_count.return_value = 0

    mock_transaction_service.get_sum_by_type.side_effect = RuntimeError("DB error on transactions")

    summary = await service.get_summary(workspace_id)
    assert summary.todos.status == "available"
    assert summary.spending.status == "unavailable"
    assert summary.investing.status == "available"

    # Case C: Investing service fails, others succeed
    mock_transaction_service.get_sum_by_type.side_effect = None
    mock_investing_service.get_summary.side_effect = Exception("Investing service down")

    summary = await service.get_summary(workspace_id)
    assert summary.todos.status == "available"
    assert summary.spending.status == "available"
    assert summary.investing.status == "unavailable"
