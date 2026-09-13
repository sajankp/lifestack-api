import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.investing.schemas import PerformanceHistoryPoint, PerformanceHistoryResponse
from app.spending.schemas import SpendPacingResponse
from app.summaries.models import MonthlySummary
from app.summaries.schemas import (
    GenerateMonthlySummaryRequest,
    MonthlySummaryResponse,
)
from app.summaries.service import WeeklySummaryService


@pytest.fixture(scope="session", autouse=True)
def override_redis_url():
    yield None


def test_monthly_summary_model_and_response_schema():
    summary_id = uuid.uuid4()
    now = datetime.now(UTC)

    summary = MonthlySummary(
        id=1,
        public_id=summary_id,
        workspace_id=1,
        month_start=date(2026, 6, 1),
        month_end=date(2026, 6, 30),
        generated_at=now,
        todo_summary={"tasks_created": 10, "tasks_completed": 8},
        spending_summary={
            "status": "complete",
            "total_income": "10000.00",
            "total_expense": "6000.00",
            "net": "4000.00",
            "currency": "USD",
        },
        investing_summary={
            "status": "complete",
            "portfolio_value_start": "50000.00",
            "portfolio_value_end": "52000.00",
            "week_change": "2000.00",
            "week_change_pct": "4.00",
            "currency": "USD",
        },
        dividend_summary={"status": "complete", "total_net": "150.00", "currency": "USD"},
        net_worth_summary={"status": "complete", "net_worth_end": "80000.00"},
        return_metrics_summary={"status": "complete", "xirr": "12.50"},
        highlights={"flags": [{"type": "info", "message": "Solid monthly close"}]},
    )

    resp = MonthlySummaryResponse.from_summary(
        summary, data_revised_after_snapshot=False, data_stale=False
    )

    assert resp.public_id == summary_id
    assert resp.month_start == date(2026, 6, 1)
    assert resp.month_end == date(2026, 6, 30)
    assert resp.todo_summary["tasks_completed"] == 8
    assert resp.spending_summary["total_income"] == "10000.00"
    assert resp.investing_summary["portfolio_value_end"] == "52000.00"
    assert resp.dividend_summary["total_net"] == "150.00"
    assert resp.is_superseded is False
    assert resp.data_stale is False


def test_generate_monthly_summary_request_validation():
    # Valid
    req = GenerateMonthlySummaryRequest(year=2026, month=6)
    assert req.year == 2026
    assert req.month == 6

    # Invalid year
    with pytest.raises(ValidationError):
        GenerateMonthlySummaryRequest(year=1999, month=6)

    with pytest.raises(ValidationError):
        GenerateMonthlySummaryRequest(year=2101, month=6)

    # Invalid month
    with pytest.raises(ValidationError):
        GenerateMonthlySummaryRequest(year=2026, month=0)

    with pytest.raises(ValidationError):
        GenerateMonthlySummaryRequest(year=2026, month=13)


def test_performance_history_schema_and_net_change():
    pt1 = PerformanceHistoryPoint(
        snapshot_date=date(2026, 8, 1),
        holdings_value=Decimal("95000.00"),
        total_cost=Decimal("90000.00"),
        total_value=Decimal("100000.00"),
        cash_value=Decimal("5000.00"),
        unrealized_gain_loss=Decimal("5000.00"),
        unrealized_gain_loss_pct=Decimal("5.56"),
    )
    pt2 = PerformanceHistoryPoint(
        snapshot_date=date(2026, 9, 1),
        holdings_value=Decimal("100000.00"),
        total_cost=Decimal("90000.00"),
        total_value=Decimal("105000.00"),
        cash_value=Decimal("5000.00"),
        unrealized_gain_loss=Decimal("10000.00"),
        unrealized_gain_loss_pct=Decimal("11.11"),
    )

    resp = PerformanceHistoryResponse(
        currency="USD",
        points=[pt1, pt2],
    )

    assert resp.currency == "USD"
    assert len(resp.points) == 2
    assert resp.points[0].total_value == Decimal("100000.00")
    assert resp.points[1].total_value == Decimal("105000.00")


def test_spend_pacing_schema_pacing_status():
    # Under budget
    resp_under = SpendPacingResponse(
        currency="USD",
        month="2026-09",
        days_in_month=30,
        days_elapsed=15,
        days_remaining=15,
        month_progress_pct=50.0,
        actual_spend=Decimal("400.00"),
        daily_burn_rate=Decimal("26.67"),
        projected_month_end_spend=Decimal("800.00"),
        total_budget=Decimal("1000.00"),
        budget_consumed_pct=40.0,
        target_pace_pct=50.0,
        pacing_delta_pct=-10.0,
        status="under_budget",
    )
    assert resp_under.status == "under_budget"
    assert resp_under.days_remaining == 15
    assert resp_under.daily_burn_rate == Decimal("26.67")

    # Over budget
    resp_over = SpendPacingResponse(
        currency="USD",
        month="2026-09",
        days_in_month=30,
        days_elapsed=15,
        days_remaining=15,
        month_progress_pct=50.0,
        actual_spend=Decimal("600.00"),
        daily_burn_rate=Decimal("40.00"),
        projected_month_end_spend=Decimal("1200.00"),
        fixed_spend=Decimal("200.00"),
        discretionary_spend=Decimal("400.00"),
        fixed_burn_rate=Decimal("13.33"),
        discretionary_burn_rate=Decimal("26.67"),
        total_budget=Decimal("1000.00"),
        budget_consumed_pct=60.0,
        target_pace_pct=50.0,
        pacing_delta_pct=10.0,
        status="over_pacing",
        categories=[
            {
                "category_id": uuid.uuid4(),
                "category_name": "Rent",
                "actual_spend": Decimal("200.00"),
                "daily_burn_rate": Decimal("13.33"),
                "projected_spend": Decimal("400.00"),
                "budget_amount": Decimal("200.00"),
                "budget_consumed_pct": 100.0,
                "pacing_status": "over_pacing",
                "is_recurring": True,
            }
        ],
    )
    assert resp_over.status == "over_pacing"
    assert resp_over.actual_spend == Decimal("600.00")
    assert resp_over.fixed_spend == Decimal("200.00")
    assert resp_over.discretionary_spend == Decimal("400.00")
    assert len(resp_over.categories) == 1
    assert resp_over.categories[0].is_recurring is True


@pytest.mark.asyncio
async def test_compose_range_date_bounds():
    # Verify _compose_range handles arbitrary date ranges
    # Create mock session that returns empty results
    session = AsyncMock()

    # Mock execute calls
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    result_mock.scalar_one_or_none.return_value = None
    result_mock.all.return_value = []
    session.execute.return_value = result_mock

    service = WeeklySummaryService(
        repository=MagicMock(), session=session, notification_service=MagicMock()
    )

    composed = await service._compose_range(
        workspace_id=1,
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 30),
        cadence_label="month",
    )

    assert "todo_summary" in composed
    assert "spending_summary" in composed
    assert "investing_summary" in composed
    assert "dividend_summary" in composed
    assert "net_worth_summary" in composed
    assert "return_metrics_summary" in composed
    assert "highlights" in composed
    assert composed["spending_summary"]["status"] in ("complete", "unavailable")
    assert composed["investing_summary"]["status"] in ("complete", "unavailable")


@pytest.mark.asyncio
async def test_monthly_summary_supersede_chain():
    """Verify that MonthlySummaryRepository.supersede correctly links old -> new -> newer
    preserving regeneration reason and timestamp while updating superseded_by_id."""
    from app.summaries.repository import MonthlySummaryRepository

    session = AsyncMock()
    session.add = MagicMock()

    id_counter = 1

    async def fake_flush():
        nonlocal id_counter
        # If any added object lacks an id, assign one
        for call_args in session.add.call_args_list:
            obj = call_args[0][0]
            if getattr(obj, "id", None) is None:
                obj.id = id_counter
                id_counter += 1

    session.flush.side_effect = fake_flush
    session.refresh = AsyncMock()

    repo = MonthlySummaryRepository(session)

    # Initial summary
    initial = MonthlySummary(
        id=1,
        public_id=uuid.uuid4(),
        workspace_id=1,
        month_start=date(2026, 6, 1),
        month_end=date(2026, 6, 30),
        todo_summary={},
        spending_summary={},
        investing_summary={},
        highlights={},
        superseded_by_id=None,
    )

    # First regeneration
    id_counter = 2
    v2 = MonthlySummary(
        id=None,
        public_id=uuid.uuid4(),
        workspace_id=1,
        month_start=date(2026, 6, 1),
        month_end=date(2026, 6, 30),
        todo_summary={},
        spending_summary={},
        investing_summary={},
        highlights={},
        superseded_by_id=None,
    )

    result_v2 = await repo.supersede(initial, v2, reason="Statement reconciled")
    assert initial.superseded_by_id == 2
    assert result_v2.id == 2
    assert result_v2.regeneration_reason == "Statement reconciled"
    assert result_v2.regenerated_at is not None
    assert result_v2.superseded_by_id is None

    # Second regeneration (chaining v2 -> v3)
    id_counter = 3
    v3 = MonthlySummary(
        id=None,
        public_id=uuid.uuid4(),
        workspace_id=1,
        month_start=date(2026, 6, 1),
        month_end=date(2026, 6, 30),
        todo_summary={},
        spending_summary={},
        investing_summary={},
        highlights={},
        superseded_by_id=None,
    )

    result_v3 = await repo.supersede(v2, v3, reason="Tax adjustments")
    assert v2.superseded_by_id == 3
    assert result_v3.id == 3
    assert result_v3.regeneration_reason == "Tax adjustments"
    assert result_v3.superseded_by_id is None
    # Verify initial still points to v2
    assert initial.superseded_by_id == 2


@pytest.mark.asyncio
async def test_spend_pacing_edge_cases_past_future_zero_budget():
    """Verify SpendPacingService edge cases: past month (100% elapsed),
    future month (0% elapsed), and zero-budget workspace."""
    from app.spending.service import BudgetService

    session = AsyncMock()
    session.add = MagicMock()  # avoid coroutine warning
    budget_repo = MagicMock()
    budget_repo.session = session
    category_repo = MagicMock()

    service = BudgetService(budget_repo, category_repo)

    # 1. Past month: 2025-01 (days_elapsed == days_in_month, days_remaining == 0)
    past_month = date(2025, 1, 1)

    # Mock SQL executions
    # execute calls:
    # 1. actual_spend sum -> 500
    # 2. fixed_spend sum -> 200
    # 3. currency_code -> "USD"
    # 4. total_budget sum -> 1000
    # 5. budgets query -> empty list
    exec_count = 0

    def mock_exec(*args, **kwargs):
        nonlocal exec_count
        exec_count += 1
        mock_result = MagicMock()
        if exec_count in (1, 2, 4):
            mock_result.scalar_one.return_value = (
                500 if exec_count == 1 else (200 if exec_count == 2 else 1000)
            )
        elif exec_count == 3:
            mock_result.scalar_one_or_none.return_value = "USD"
        else:
            mock_result.scalars.return_value.all.return_value = []
        return mock_result

    session.execute.side_effect = AsyncMock(side_effect=mock_exec)

    res_past = await service.get_spend_pacing(workspace_id=1, target_month=past_month)
    assert res_past.days_elapsed == 31
    assert res_past.days_remaining == 0
    assert res_past.month_progress_pct == 100.0
    assert res_past.actual_spend == Decimal("500")
    assert res_past.fixed_spend == Decimal("200")
    assert res_past.discretionary_spend == Decimal("300")

    # 2. Future month: 2030-01 (days_elapsed == 0, days_remaining == 31)
    future_month = date(2030, 1, 1)
    exec_count = 0
    res_future = await service.get_spend_pacing(workspace_id=1, target_month=future_month)
    assert res_future.days_elapsed == 0
    assert res_future.days_remaining == 31
    assert res_future.month_progress_pct == 0.0

    # 3. Zero budget: total_budget == 0
    exec_count = 0

    def mock_exec_zero_budget(*args, **kwargs):
        nonlocal exec_count
        exec_count += 1
        mock_result = MagicMock()
        if exec_count in (1, 2):
            mock_result.scalar_one.return_value = 0
        elif exec_count == 3:
            mock_result.scalar_one_or_none.return_value = "USD"
        elif exec_count == 4:
            mock_result.scalar_one.return_value = 0  # 0 budget
        else:
            mock_result.scalars.return_value.all.return_value = []
        return mock_result

    session.execute.side_effect = AsyncMock(side_effect=mock_exec_zero_budget)
    res_zero = await service.get_spend_pacing(workspace_id=1, target_month=past_month)
    assert res_zero.total_budget is None
    assert res_zero.budget_consumed_pct is None
    assert res_zero.status == "no_budget"

