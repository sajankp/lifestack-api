import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.workflows import MorningBriefingWorkflow
from app.dashboard.schemas import BriefingResponse


def _make_todo(*, title="Overdue thing", due_date=None):
    todo = MagicMock()
    todo.public_id = uuid.uuid4()
    todo.title = title
    todo.due_date = due_date
    return todo


@pytest.fixture
def mock_todo_service():
    svc = AsyncMock()
    svc.get_summary_counts.return_value = (0, 0)
    svc.get_overdue_items.return_value = []
    svc.get_next_due_items.return_value = []
    svc.get_recurring_rules_due_between.return_value = []
    return svc


@pytest.fixture
def mock_budget_service():
    svc = AsyncMock()
    svc.get_budget_performance.return_value = MagicMock(groups=[])
    return svc


@pytest.fixture
def mock_investing_service():
    svc = AsyncMock()
    svc.summary.return_value = MagicMock(
        daily_change=None, daily_change_pct=None, valuation_status="current"
    )
    return svc


@pytest.fixture
def mock_recurring_transaction_service():
    svc = AsyncMock()
    svc.get_due_between.return_value = []
    return svc


@pytest.fixture
def mock_notification_service():
    svc = AsyncMock()
    svc.list_recent_unread.return_value = []
    return svc


@pytest.fixture
def mock_import_repo():
    repo = AsyncMock()
    repo.list_pending_review.return_value = ([], 0)
    return repo


@pytest.fixture
def mock_weekly_summary_repo():
    repo = AsyncMock()
    repo.latest.return_value = None
    return repo


@pytest.fixture
def mock_finance_setting_repo():
    repo = AsyncMock()
    repo.get_by_workspace.return_value = MagicMock(reporting_currency_code="INR")
    return repo


@pytest.fixture
def workflow(
    mock_todo_service,
    mock_budget_service,
    mock_investing_service,
    mock_recurring_transaction_service,
    mock_notification_service,
    mock_import_repo,
    mock_weekly_summary_repo,
    mock_finance_setting_repo,
):
    return MorningBriefingWorkflow(
        todo_service=mock_todo_service,
        budget_service=mock_budget_service,
        investing_performance_service=mock_investing_service,
        recurring_transaction_service=mock_recurring_transaction_service,
        notification_service=mock_notification_service,
        import_repo=mock_import_repo,
        weekly_summary_repo=mock_weekly_summary_repo,
        finance_setting_repo=mock_finance_setting_repo,
    )


@pytest.mark.asyncio
async def test_all_clear_when_everything_empty(workflow):
    result = await workflow.get_briefing(workspace_id=1, user_id=1)
    assert isinstance(result, BriefingResponse)
    assert result.all_clear is True
    assert result.lines == []
    assert result.reporting_currency == "INR"


@pytest.mark.asyncio
async def test_overdue_todos_line_is_critical(workflow, mock_todo_service):
    mock_todo_service.get_summary_counts.return_value = (5, 2)
    top = _make_todo(title="Pay rent")
    mock_todo_service.get_overdue_items.return_value = [top]

    result = await workflow.get_briefing(workspace_id=1, user_id=1)

    assert result.all_clear is False
    line = result.lines[0]
    assert line.severity == "critical"
    assert "2 overdue" in line.text
    assert "Pay rent" in line.text
    assert line.source.entity_type == "todo"
    assert line.source.entity_public_id == str(top.public_id)
    assert line.source.route == "/todo"


@pytest.mark.asyncio
async def test_due_today_todos_line_is_warning(workflow, mock_todo_service):
    now = datetime.now(UTC)
    due_today_item = _make_todo(title="Call bank", due_date=now.replace(hour=18, minute=0))
    due_tomorrow_item = _make_todo(
        title="Later thing", due_date=now.replace(hour=18, minute=0) + timedelta(days=1)
    )
    mock_todo_service.get_next_due_items.return_value = [due_today_item, due_tomorrow_item]

    result = await workflow.get_briefing(workspace_id=1, user_id=1)

    warning_lines = [line for line in result.lines if line.severity == "warning"]
    assert len(warning_lines) == 1
    assert "Call bank" in warning_lines[0].text
    assert "1 task" in warning_lines[0].text


@pytest.mark.asyncio
async def test_budget_guardrail_thresholds(workflow, mock_budget_service):
    warning_group = MagicMock(
        category_group_id=uuid.uuid4(),
        category_group_name="Groceries",
        budget_amount=Decimal("500"),
        utilization_pct=90.0,
    )
    critical_group = MagicMock(
        category_group_id=uuid.uuid4(),
        category_group_name="Dining",
        budget_amount=Decimal("200"),
        utilization_pct=120.0,
    )
    below_threshold_group = MagicMock(
        category_group_id=uuid.uuid4(),
        category_group_name="Utilities",
        budget_amount=Decimal("100"),
        utilization_pct=50.0,
    )
    no_budget_group = MagicMock(
        category_group_id=uuid.uuid4(),
        category_group_name="Misc",
        budget_amount=None,
        utilization_pct=999.0,
    )
    mock_budget_service.get_budget_performance.return_value = MagicMock(
        groups=[warning_group, critical_group, below_threshold_group, no_budget_group]
    )

    result = await workflow.get_briefing(workspace_id=1, user_id=1)

    texts_by_severity = {line.severity: line.text for line in result.lines}
    assert "Groceries" in texts_by_severity["warning"]
    assert "Dining" in texts_by_severity["critical"]
    assert not any("Utilities" in line.text for line in result.lines)
    assert not any("Misc" in line.text for line in result.lines)


@pytest.mark.asyncio
async def test_recurring_due_lines(workflow, mock_recurring_transaction_service, mock_todo_service):
    today = datetime.now(UTC).date()
    tx_rule = MagicMock(
        public_id=uuid.uuid4(),
        type="expense",
        description="Rent",
        amount=Decimal("1500.00"),
        next_due_date=today,
    )
    mock_recurring_transaction_service.get_due_between.return_value = [(tx_rule, "Housing")]
    todo_rule = MagicMock(public_id=uuid.uuid4(), title="Water the plants", next_due_date=today)
    mock_todo_service.get_recurring_rules_due_between.return_value = [todo_rule]

    result = await workflow.get_briefing(workspace_id=1, user_id=1)

    info_texts = [line.text for line in result.lines if line.severity == "info"]
    assert any("Rent" in text for text in info_texts)
    assert any("Water the plants" in text for text in info_texts)


@pytest.mark.asyncio
async def test_net_worth_line_degraded_valuation_is_warning(workflow, mock_investing_service):
    mock_investing_service.summary.return_value = MagicMock(
        daily_change=Decimal("-120.50"),
        daily_change_pct=Decimal("-1.20"),
        valuation_status="partial",
    )

    result = await workflow.get_briefing(workspace_id=1, user_id=1)

    line = next(line for line in result.lines if line.source.route == "/investing")
    assert line.severity == "warning"
    assert "partial" in line.text


@pytest.mark.asyncio
async def test_net_worth_line_current_valuation_is_info(workflow, mock_investing_service):
    mock_investing_service.summary.return_value = MagicMock(
        daily_change=Decimal("50.00"),
        daily_change_pct=Decimal("0.50"),
        valuation_status="current",
    )

    result = await workflow.get_briefing(workspace_id=1, user_id=1)

    line = next(line for line in result.lines if line.source.route == "/investing")
    assert line.severity == "info"


@pytest.mark.asyncio
async def test_pending_review_line(workflow, mock_import_repo):
    batch = MagicMock(public_id=uuid.uuid4())
    mock_import_repo.list_pending_review.return_value = ([batch], 3)

    result = await workflow.get_briefing(workspace_id=1, user_id=1)

    line = next(line for line in result.lines if line.source.route == "/imports")
    assert line.severity == "warning"
    assert "3 imports" in line.text


@pytest.mark.asyncio
async def test_weekly_summary_within_freshness_window(workflow, mock_weekly_summary_repo):
    mock_weekly_summary_repo.latest.return_value = MagicMock(
        generated_at=datetime.now(UTC) - timedelta(hours=10),
        week_start=datetime(2026, 7, 6).date(),
        public_id=uuid.uuid4(),
    )

    result = await workflow.get_briefing(workspace_id=1, user_id=1)

    line = next(line for line in result.lines if line.source.route == "/summaries")
    assert line.severity == "info"
    assert "2026-07-06" in line.text


@pytest.mark.asyncio
async def test_weekly_summary_outside_freshness_window_omitted(workflow, mock_weekly_summary_repo):
    mock_weekly_summary_repo.latest.return_value = MagicMock(
        generated_at=datetime.now(UTC) - timedelta(hours=72),
        week_start=datetime(2026, 6, 29).date(),
        public_id=uuid.uuid4(),
    )

    result = await workflow.get_briefing(workspace_id=1, user_id=1)

    assert not any(line.source.route == "/summaries" for line in result.lines)


@pytest.mark.asyncio
async def test_fresh_insight_lines_inherit_severity(workflow, mock_notification_service):
    notification = MagicMock(
        severity="warning",
        title="Groceries spending is up this week",
        entity_type="spending_category_anomaly",
        entity_public_id=uuid.uuid4(),
    )
    mock_notification_service.list_recent_unread.return_value = [notification]

    result = await workflow.get_briefing(workspace_id=1, user_id=1)

    line = next(
        line for line in result.lines if line.source.entity_type == "spending_category_anomaly"
    )
    assert line.severity == "warning"
    assert line.source.route == "/spending"
    assert line.text == "Groceries spending is up this week"


@pytest.mark.asyncio
async def test_ordering_severity_then_domain(
    workflow, mock_todo_service, mock_budget_service, mock_import_repo
):
    # Critical overdue todos (domain 0) should sort before a critical budget
    # breach (domain 2) even though both are severity="critical".
    mock_todo_service.get_summary_counts.return_value = (1, 1)
    mock_todo_service.get_overdue_items.return_value = [_make_todo()]
    mock_budget_service.get_budget_performance.return_value = MagicMock(
        groups=[
            MagicMock(
                category_group_id=uuid.uuid4(),
                category_group_name="Dining",
                budget_amount=Decimal("100"),
                utilization_pct=150.0,
            )
        ]
    )
    mock_import_repo.list_pending_review.return_value = ([], 2)

    result = await workflow.get_briefing(workspace_id=1, user_id=1)

    severities_and_routes = [(line.severity, line.source.route) for line in result.lines]
    critical_positions = [
        i for i, (sev, _) in enumerate(severities_and_routes) if sev == "critical"
    ]
    warning_positions = [i for i, (sev, _) in enumerate(severities_and_routes) if sev == "warning"]
    assert max(critical_positions) < min(warning_positions)
    assert severities_and_routes[0] == ("critical", "/todo")


@pytest.mark.asyncio
async def test_caps_at_ten_lines_with_overflow_note(workflow, mock_notification_service):
    notifications = [
        MagicMock(
            severity="info",
            title=f"Insight {i}",
            entity_type=None,
            entity_public_id=None,
        )
        for i in range(12)
    ]
    mock_notification_service.list_recent_unread.return_value = notifications

    result = await workflow.get_briefing(workspace_id=1, user_id=1)

    assert len(result.lines) == 10
    assert result.lines[-1].text.startswith("...and")
    assert result.lines[-1].source.route == "/notifications"


@pytest.mark.asyncio
async def test_section_failure_degrades_to_omission(workflow, mock_todo_service, mock_import_repo):
    mock_todo_service.get_summary_counts.side_effect = RuntimeError("todo db down")
    mock_import_repo.list_pending_review.return_value = ([], 1)

    result = await workflow.get_briefing(workspace_id=1, user_id=1)

    # The todo section failed, but the import section still contributes —
    # one failing domain must not blank the whole briefing.
    assert any(line.source.route == "/imports" for line in result.lines)
    assert result.all_clear is False


def _make_dose_slot(*, status="pending"):
    slot = MagicMock()
    slot.status = status
    return slot


def _make_weight_trend(*, entries=None, delta_7d_kg=None):
    trend = MagicMock()
    trend.entries = entries or []
    trend.delta_7d_kg = delta_7d_kg
    return trend


@pytest.fixture
def mock_health_service():
    svc = AsyncMock()
    svc.get_schedule.return_value = []
    svc.get_weight_trend.return_value = _make_weight_trend()
    return svc


def _workflow_with_health(
    mock_todo_service,
    mock_budget_service,
    mock_investing_service,
    mock_recurring_transaction_service,
    mock_notification_service,
    mock_import_repo,
    mock_weekly_summary_repo,
    mock_finance_setting_repo,
    mock_health_service,
):
    return MorningBriefingWorkflow(
        todo_service=mock_todo_service,
        budget_service=mock_budget_service,
        investing_performance_service=mock_investing_service,
        recurring_transaction_service=mock_recurring_transaction_service,
        notification_service=mock_notification_service,
        import_repo=mock_import_repo,
        weekly_summary_repo=mock_weekly_summary_repo,
        finance_setting_repo=mock_finance_setting_repo,
        health_service=mock_health_service,
    )


@pytest.mark.asyncio
async def test_health_service_none_omits_health_lines(workflow):
    # The shared `workflow` fixture doesn't wire health_service (defaults to
    # None) — the health line type must degrade to omission, not error.
    result = await workflow.get_briefing(workspace_id=1, user_id=1)
    assert result.all_clear is True


@pytest.mark.asyncio
async def test_doses_due_today_line_is_info_when_none_missed(
    mock_todo_service,
    mock_budget_service,
    mock_investing_service,
    mock_recurring_transaction_service,
    mock_notification_service,
    mock_import_repo,
    mock_weekly_summary_repo,
    mock_finance_setting_repo,
    mock_health_service,
):
    # An already-taken dose is resolved and must NOT count toward "due today":
    # only the still-outstanding (pending/missed) slot does.
    mock_health_service.get_schedule.side_effect = [
        [
            _make_dose_slot(status="pending"),
            _make_dose_slot(status="taken"),
            _make_dose_slot(status="missed"),
        ],  # today
        [],  # yesterday
    ]
    wf = _workflow_with_health(
        mock_todo_service,
        mock_budget_service,
        mock_investing_service,
        mock_recurring_transaction_service,
        mock_notification_service,
        mock_import_repo,
        mock_weekly_summary_repo,
        mock_finance_setting_repo,
        mock_health_service,
    )

    result = await wf.get_briefing(workspace_id=1, user_id=1)

    health_lines = [line for line in result.lines if line.source.route == "/health"]
    assert len(health_lines) == 1
    assert health_lines[0].severity == "info"
    # pending + missed count as outstanding; the taken dose is excluded.
    assert "2 doses due today" in health_lines[0].text


@pytest.mark.asyncio
async def test_all_doses_taken_today_omits_health_line(
    mock_todo_service,
    mock_budget_service,
    mock_investing_service,
    mock_recurring_transaction_service,
    mock_notification_service,
    mock_import_repo,
    mock_weekly_summary_repo,
    mock_finance_setting_repo,
    mock_health_service,
):
    # Every scheduled dose today is resolved (taken/skipped) and nothing was
    # missed yesterday — the "due today" line must disappear entirely, not
    # keep reporting resolved doses as still due.
    mock_health_service.get_schedule.side_effect = [
        [_make_dose_slot(status="taken"), _make_dose_slot(status="skipped")],  # today
        [],  # yesterday
    ]
    wf = _workflow_with_health(
        mock_todo_service,
        mock_budget_service,
        mock_investing_service,
        mock_recurring_transaction_service,
        mock_notification_service,
        mock_import_repo,
        mock_weekly_summary_repo,
        mock_finance_setting_repo,
        mock_health_service,
    )

    result = await wf.get_briefing(workspace_id=1, user_id=1)

    health_lines = [line for line in result.lines if line.source.route == "/health"]
    assert health_lines == []


@pytest.mark.asyncio
async def test_missed_yesterday_line_is_warning(
    mock_todo_service,
    mock_budget_service,
    mock_investing_service,
    mock_recurring_transaction_service,
    mock_notification_service,
    mock_import_repo,
    mock_weekly_summary_repo,
    mock_finance_setting_repo,
    mock_health_service,
):
    mock_health_service.get_schedule.side_effect = [
        [],  # today
        [_make_dose_slot(status="missed")],  # yesterday
    ]
    wf = _workflow_with_health(
        mock_todo_service,
        mock_budget_service,
        mock_investing_service,
        mock_recurring_transaction_service,
        mock_notification_service,
        mock_import_repo,
        mock_weekly_summary_repo,
        mock_finance_setting_repo,
        mock_health_service,
    )

    result = await wf.get_briefing(workspace_id=1, user_id=1)

    health_lines = [line for line in result.lines if line.source.route == "/health"]
    assert len(health_lines) == 1
    assert health_lines[0].severity == "warning"
    assert "1 missed yesterday" in health_lines[0].text


@pytest.mark.asyncio
async def test_weight_weekly_move_line_requires_two_entries_in_7_days(
    mock_todo_service,
    mock_budget_service,
    mock_investing_service,
    mock_recurring_transaction_service,
    mock_notification_service,
    mock_import_repo,
    mock_weekly_summary_repo,
    mock_finance_setting_repo,
    mock_health_service,
):
    now = datetime.now(UTC)
    entry_a = MagicMock(measured_at=now - timedelta(days=6), weight_kg=Decimal("80.0"))
    entry_b = MagicMock(measured_at=now, weight_kg=Decimal("79.6"))
    mock_health_service.get_weight_trend.return_value = _make_weight_trend(
        entries=[entry_a, entry_b], delta_7d_kg=Decimal("-0.4")
    )
    wf = _workflow_with_health(
        mock_todo_service,
        mock_budget_service,
        mock_investing_service,
        mock_recurring_transaction_service,
        mock_notification_service,
        mock_import_repo,
        mock_weekly_summary_repo,
        mock_finance_setting_repo,
        mock_health_service,
    )

    result = await wf.get_briefing(workspace_id=1, user_id=1)

    weight_lines = [line for line in result.lines if "weight" in line.text]
    assert len(weight_lines) == 1
    assert weight_lines[0].severity == "info"
    assert "-0.4 kg this week" in weight_lines[0].text


@pytest.mark.asyncio
async def test_weight_line_omitted_with_fewer_than_two_entries(
    mock_todo_service,
    mock_budget_service,
    mock_investing_service,
    mock_recurring_transaction_service,
    mock_notification_service,
    mock_import_repo,
    mock_weekly_summary_repo,
    mock_finance_setting_repo,
    mock_health_service,
):
    now = datetime.now(UTC)
    entry_a = MagicMock(measured_at=now, weight_kg=Decimal("79.6"))
    mock_health_service.get_weight_trend.return_value = _make_weight_trend(
        entries=[entry_a], delta_7d_kg=None
    )
    wf = _workflow_with_health(
        mock_todo_service,
        mock_budget_service,
        mock_investing_service,
        mock_recurring_transaction_service,
        mock_notification_service,
        mock_import_repo,
        mock_weekly_summary_repo,
        mock_finance_setting_repo,
        mock_health_service,
    )

    result = await wf.get_briefing(workspace_id=1, user_id=1)

    assert not any("weight" in line.text for line in result.lines)
