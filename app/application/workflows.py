import asyncio
import calendar
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import structlog
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.repository import AuthSessionRepository, UserRepository
from app.auth.schemas import UserCreate
from app.auth.service import AuthService
from app.config import settings
from app.core.audit import AuditLogger, snapshot_columns
from app.core.recurrence import advance_due_date
from app.dashboard.schemas import (
    BriefingLine,
    BriefingResponse,
    BriefingSource,
    DashboardSummary,
    InvestingSummary,
    SpendingSummary,
    SystemSummary,
    TodosSummary,
)
from app.exports.models import ExportRecord, ExportStatus
from app.exports.repository import ExportRepository
from app.exports.service import ExportService
from app.finance.repository import (
    AccountRepository,
    CurrencyRepository,
    FinanceSettingRepository,
    FxRateRepository,
)
from app.finance.schemas import FxRateUpsert
from app.finance.service import FxRateService
from app.health.repository import MedicationRepository
from app.health.schedule import get_dose_slots_in_window
from app.health.service import HealthService
from app.imports.models import ImportBatch, ImportPreviewRow
from app.imports.repository import ImportRepository
from app.investing.performance_service import PerformanceService
from app.notifications.email import send_email
from app.notifications.models import Notification
from app.notifications.push import send_web_push
from app.notifications.repository import NotificationRepository, PushSubscriptionRepository
from app.notifications.service import NotificationService
from app.platform.models import Workspace, WorkspaceMembership
from app.platform.service import WorkspaceService
from app.spending.models import (
    RecurringTransaction,
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
    TransactionType,
)
from app.spending.repository import (
    CategoryGroupRepository,
    CategoryRepository,
    KpiRepository,
)
from app.spending.repository import TransactionRepository as SpendingTransactionRepository
from app.spending.schemas import BudgetSpotlightItem
from app.spending.service import (
    BudgetService,
    CategoryService,
    KpiService,
    RecurringTransactionService,
    TransactionService,
)
from app.summaries.repository import WeeklySummaryRepository
from app.todo.models import PriorityEnum, RecurringTodoRule, Todo
from app.todo.repository import TodoRepository
from app.todo.service import _TODO_AUDIT_FIELDS, TodoService

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Registration Workflow
# ---------------------------------------------------------------------------


class UserRegistrationWorkflow:
    def __init__(
        self,
        auth_service: AuthService,
        workspace_service: WorkspaceService,
        category_service: CategoryService,
    ):
        self.auth_service = auth_service
        self.workspace_service = workspace_service
        self.category_service = category_service

    async def register_user_with_workspace(self, user_in: UserCreate) -> bool:
        """Register a user, provision their default workspace, and seed system spending categories."""
        user = await self.auth_service.register_user(user_in)

        # Provision default workspace
        workspace = await self.workspace_service.ensure_default_workspace(
            user_id=user.id, username=user.username
        )

        # Atomically seed default spending categories for the new workspace
        await self.category_service.provision_default_categories(workspace.id)  # type: ignore[arg-type]

        return True


class DashboardSummaryWorkflow:
    def __init__(
        self,
        todo_service: TodoService,
        transaction_service: TransactionService,
        budget_service: BudgetService,
        investing_performance_service: PerformanceService,
    ):
        self.todo_service = todo_service
        self.transaction_service = transaction_service
        self.budget_service = budget_service
        self.investing_performance_service = investing_performance_service

    async def get_summary(self, workspace_id: int) -> DashboardSummary:
        now = datetime.now(UTC)

        todos_res = TodosSummary()
        try:
            open_count, overdue_count = await self.todo_service.get_summary_counts(
                workspace_id, now
            )
            next_due_items = await self.todo_service.get_next_due_items(workspace_id, now, limit=5)
            active_guardrail_todo_count = await self.todo_service.get_active_guardrail_todo_count(
                workspace_id
            )
            todos_res = TodosSummary(
                status="available",
                open_count=open_count,
                overdue_count=overdue_count,
                next_due_items=[
                    {
                        "public_id": str(todo.public_id),
                        "title": todo.title,
                        "due_date": todo.due_date.isoformat() if todo.due_date else None,
                        "priority": todo.priority,
                    }
                    for todo in next_due_items
                ],
                active_guardrail_todo_count=active_guardrail_todo_count,
            )
        except Exception:
            logger.exception("dashboard_todos_fetch_failed", workspace_id=workspace_id)
            todos_res = TodosSummary(status="unavailable")

        spending_res = SpendingSummary()
        try:
            start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            category_totals = await self.transaction_service.get_category_totals(
                workspace_id=workspace_id,
                from_date=start_of_month,
                to_date=now,
                type_filter=TransactionType.expense,
            )
            budgets, _ = await self.budget_service.list_budgets(
                workspace_id=workspace_id,
                month_start=start_of_month.date(),
                limit=5000,
                offset=0,
            )
            month_spent = sum(category_totals.values(), Decimal("0"))
            budget_amount_by_category = {
                budget.category_id: budget.amount
                for budget in budgets
                if budget.category_id is not None
            }
            top_overspent_categories = []
            for category_id, spent in category_totals.items():
                budget_amount = budget_amount_by_category.get(category_id)
                if not budget_amount or budget_amount <= 0:
                    continue
                overspend = spent - budget_amount
                if overspend <= 0:
                    continue
                ratio = spent / budget_amount
                top_overspent_categories.append({
                    "category_id": category_id,
                    "spent": spent,
                    "budget": budget_amount,
                    "overspend": overspend,
                    "ratio": ratio,
                })
            top_overspent_categories.sort(key=lambda item: item["overspend"], reverse=True)

            perf = await self.budget_service.get_budget_performance(
                workspace_id, start_of_month.date(), start_of_month.date()
            )

            _, last_day = calendar.monthrange(now.year, now.month)
            days_remaining = max(1, last_day - now.day + 1)

            spotlight_items = []
            for item in perf.groups:
                if item.budget_amount is None or item.budget_amount == Decimal("0"):
                    continue

                remaining = item.remaining or Decimal("0")
                daily_amount_left = max(Decimal("0"), remaining / Decimal(str(days_remaining)))

                spotlight_items.append(
                    BudgetSpotlightItem(
                        category_group_id=item.category_group_id,
                        category_group_name=item.category_group_name,
                        budget_amount=item.budget_amount,
                        actual_amount=item.actual_amount,
                        utilization_pct=item.utilization_pct or 0.0,
                        remaining=remaining,
                        status=item.status,
                        daily_amount_left=daily_amount_left,
                    )
                )

            spotlight_items.sort(key=lambda x: x.utilization_pct, reverse=True)

            spending_res = SpendingSummary(
                status="available",
                month_spent=month_spent,
                budget_spotlight=spotlight_items[:2],
                top_overspent_categories=top_overspent_categories[:5],
            )
        except Exception:
            logger.exception("dashboard_spending_fetch_failed", workspace_id=workspace_id)
            spending_res = SpendingSummary(status="unavailable")

        investing_res = InvestingSummary()
        try:
            performance = await self.investing_performance_service.summary(workspace_id)
            investing_res = InvestingSummary(
                status="available",
                portfolio_value=performance.portfolio_value,
                invested_value=performance.invested_value,
                total_gain_loss=performance.total_gain_loss,
                total_gain_loss_pct=performance.total_gain_loss_pct,
                daily_change=performance.daily_change,
                daily_change_pct=performance.daily_change_pct,
                snapshot_date=performance.snapshot_date,
                previous_snapshot_date=performance.previous_snapshot_date,
                valuation_status=performance.valuation_status,
                holdings_count=performance.holdings_count,
                cash_total=performance.cash_total,
            )
        except Exception:
            logger.exception("dashboard_investing_fetch_failed", workspace_id=workspace_id)
            investing_res = InvestingSummary(status="unavailable")

        return DashboardSummary(
            todos=todos_res,
            spending=spending_res,
            investing=investing_res,
            system=SystemSummary(generated_at=now),
        )


# ---------------------------------------------------------------------------
# Morning Briefing Workflow (spec-067)
# ---------------------------------------------------------------------------

_BRIEFING_MAX_LINES = 10
_BRIEFING_FRESH_INSIGHT_HOURS = 48
_BRIEFING_WEEKLY_SUMMARY_FRESH_HOURS = 48
_BRIEFING_BUDGET_WARNING_PCT = 85.0
_BRIEFING_BUDGET_CRITICAL_PCT = 100.0
_BRIEFING_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

# Line-type domain order (spec-067 table) — the fixed tiebreak used once two
# lines share the same severity. Health (spec-069) sits after todos, before
# budget lines — "health is the day's 'life' half" (spec-069 §C).
_DOMAIN_OVERDUE_TODOS = 0
_DOMAIN_DUE_TODAY_TODOS = 1
_DOMAIN_HEALTH = 2
_DOMAIN_BUDGET_GUARDRAILS = 3
_DOMAIN_RECURRING_DUE = 4
_DOMAIN_NET_WORTH = 5
_DOMAIN_PENDING_REVIEW = 6
_DOMAIN_WEEKLY_SUMMARY = 7
_DOMAIN_FRESH_INSIGHTS = 8

_INSIGHT_ENTITY_ROUTES = {
    "spending_category_anomaly": "/spending",
    "spending_budget_pace": "/spending",
    "spending_category_recurring": "/spending",
}


class MorningBriefingWorkflow:
    """Composes the deterministic morning briefing (spec-067): one ordered,
    severity-ranked list over existing read models, zero LLM involvement.
    Each line type is built independently and degrades to omission on
    failure — the same per-section isolation as ``DashboardSummaryWorkflow``
    — so one failing domain never blanks the whole briefing."""

    def __init__(
        self,
        todo_service: TodoService,
        budget_service: BudgetService,
        investing_performance_service: PerformanceService,
        recurring_transaction_service: RecurringTransactionService,
        notification_service: NotificationService,
        import_repo: ImportRepository,
        weekly_summary_repo: WeeklySummaryRepository,
        finance_setting_repo: FinanceSettingRepository,
        health_service: HealthService | None = None,
    ):
        self.todo_service = todo_service
        self.budget_service = budget_service
        self.investing_performance_service = investing_performance_service
        self.recurring_transaction_service = recurring_transaction_service
        self.notification_service = notification_service
        self.import_repo = import_repo
        self.weekly_summary_repo = weekly_summary_repo
        self.finance_setting_repo = finance_setting_repo
        self.health_service = health_service

    async def get_briefing(self, workspace_id: int, user_id: int) -> BriefingResponse:
        now = datetime.now(UTC)
        raw_lines: list[tuple[int, BriefingLine]] = []

        builders = [
            (_DOMAIN_OVERDUE_TODOS, self._overdue_todo_lines(workspace_id, now)),
            (_DOMAIN_DUE_TODAY_TODOS, self._due_today_todo_lines(workspace_id, now)),
            (_DOMAIN_HEALTH, self._health_lines(workspace_id, now)),
            (_DOMAIN_BUDGET_GUARDRAILS, self._budget_guardrail_lines(workspace_id, now)),
            (_DOMAIN_RECURRING_DUE, self._recurring_due_lines(workspace_id, now)),
            (_DOMAIN_NET_WORTH, self._net_worth_lines(workspace_id)),
            (_DOMAIN_PENDING_REVIEW, self._pending_review_lines(workspace_id)),
            (_DOMAIN_WEEKLY_SUMMARY, self._weekly_summary_lines(workspace_id, now)),
            (_DOMAIN_FRESH_INSIGHTS, self._fresh_insight_lines(workspace_id, user_id, now)),
        ]
        for domain_order, coro in builders:
            try:
                lines = await coro
            except Exception:
                logger.exception(
                    "morning_briefing_line_failed", workspace_id=workspace_id, domain=domain_order
                )
                continue
            for line in lines:
                raw_lines.append((domain_order, line))

        def _sort_key(item: tuple[int, BriefingLine]):
            domain_order, line = item
            pid = line.source.entity_public_id
            return (
                _BRIEFING_SEVERITY_RANK.get(line.severity, 3),
                domain_order,
                0 if pid else 1,
                pid or "",
                line.source.route,
                line.text,
            )

        raw_lines.sort(key=_sort_key)
        lines = [line for _, line in raw_lines]

        if len(lines) > _BRIEFING_MAX_LINES:
            overflow_count = len(lines) - (_BRIEFING_MAX_LINES - 1)
            lines = lines[: _BRIEFING_MAX_LINES - 1]
            lines.append(
                BriefingLine(
                    severity="info",
                    text=f"...and {overflow_count} more item{'s' if overflow_count != 1 else ''}",
                    source=BriefingSource(route="/notifications"),
                )
            )

        reporting_currency = "USD"
        try:
            setting = await self.finance_setting_repo.get_by_workspace(workspace_id)
            if setting and setting.reporting_currency_code:
                reporting_currency = setting.reporting_currency_code
        except Exception:
            logger.exception("morning_briefing_currency_lookup_failed", workspace_id=workspace_id)

        return BriefingResponse(
            generated_at=now,
            all_clear=len(lines) == 0,
            reporting_currency=reporting_currency,
            lines=lines,
        )

    async def _overdue_todo_lines(self, workspace_id: int, now: datetime) -> list[BriefingLine]:
        _open_count, overdue_count = await self.todo_service.get_summary_counts(workspace_id, now)
        if overdue_count <= 0:
            return []
        top_items = await self.todo_service.get_overdue_items(workspace_id, now, limit=1)
        top = top_items[0] if top_items else None
        plural = "s" if overdue_count != 1 else ""
        text = (
            f'{overdue_count} overdue task{plural} — top: "{top.title}"'
            if top
            else (f"{overdue_count} overdue task{plural}")
        )
        return [
            BriefingLine(
                severity="critical",
                text=text,
                source=BriefingSource(
                    entity_type="todo",
                    entity_public_id=str(top.public_id) if top else None,
                    route="/todo",
                ),
            )
        ]

    async def _due_today_todo_lines(self, workspace_id: int, now: datetime) -> list[BriefingLine]:
        upcoming = await self.todo_service.get_next_due_items(workspace_id, now, limit=20)
        today = now.date()
        due_today = [item for item in upcoming if item.due_date and item.due_date.date() == today]
        if not due_today:
            return []
        top = due_today[0]
        plural = "s" if len(due_today) != 1 else ""
        return [
            BriefingLine(
                severity="warning",
                text=f'{len(due_today)} task{plural} due today — next: "{top.title}"',
                source=BriefingSource(
                    entity_type="todo", entity_public_id=str(top.public_id), route="/todo"
                ),
            )
        ]

    async def _health_lines(self, workspace_id: int, now: datetime) -> list[BriefingLine]:
        """Two Health Memory line types (spec-069 §C): (a) doses due today /
        missed yesterday, (b) weight weekly move. Facts only — no advice, per
        the trust boundary. No-op when health_service isn't wired (feature
        can be toggled off without touching the workflow signature)."""
        if self.health_service is None:
            return []
        today = now.date()
        yesterday = today - timedelta(days=1)
        today_slots = await self.health_service.get_schedule(workspace_id, today)
        yesterday_slots = await self.health_service.get_schedule(workspace_id, yesterday)
        missed_yesterday = sum(1 for s in yesterday_slots if s.status == "missed")

        # "Due today" means still outstanding — a dose already logged as taken or
        # skipped is resolved and must drop off the briefing (a taken dose kept
        # showing "due today" for the rest of the day). pending/missed remain, as
        # a missed dose can still be logged late.
        due_today = sum(1 for s in today_slots if s.status not in ("taken", "skipped"))

        lines: list[BriefingLine] = []
        if due_today or missed_yesterday:
            parts = []
            if due_today:
                plural = "s" if due_today != 1 else ""
                parts.append(f"{due_today} dose{plural} due today")
            if missed_yesterday:
                plural = "s" if missed_yesterday != 1 else ""
                parts.append(f"{missed_yesterday} missed yesterday")
            lines.append(
                BriefingLine(
                    severity="warning" if missed_yesterday else "info",
                    text=", ".join(parts),
                    source=BriefingSource(route="/health"),
                )
            )

        trend = await self.health_service.get_weight_trend(workspace_id, days=30)
        week_ago = now - timedelta(days=7)
        recent_entries = [e for e in trend.entries if e.measured_at >= week_ago]
        if len(recent_entries) >= 2 and trend.delta_7d_kg is not None:
            sign = "+" if trend.delta_7d_kg >= 0 else ""
            lines.append(
                BriefingLine(
                    severity="info",
                    text=f"weight {sign}{trend.delta_7d_kg:.1f} kg this week",
                    source=BriefingSource(route="/health"),
                )
            )
        return lines

    async def _budget_guardrail_lines(self, workspace_id: int, now: datetime) -> list[BriefingLine]:
        today = now.date()
        month_start = today.replace(day=1)
        perf = await self.budget_service.get_budget_performance(
            workspace_id, month_start, month_start
        )
        _, days_in_month = calendar.monthrange(today.year, today.month)
        days_remaining = max(1, days_in_month - today.day + 1)

        lines: list[BriefingLine] = []
        for item in perf.groups:
            if item.budget_amount is None or item.budget_amount == Decimal("0"):
                continue
            utilization_pct = item.utilization_pct or 0.0
            if utilization_pct < _BRIEFING_BUDGET_WARNING_PCT:
                continue
            severity = "critical" if utilization_pct >= _BRIEFING_BUDGET_CRITICAL_PCT else "warning"
            day_word = "day" if days_remaining == 1 else "days"
            lines.append(
                BriefingLine(
                    severity=severity,
                    text=(
                        f"{item.category_group_name} budget at {utilization_pct:.0f}% "
                        f"with {days_remaining} {day_word} left"
                    ),
                    source=BriefingSource(
                        entity_type="budget_group",
                        entity_public_id=str(item.category_group_id)
                        if item.category_group_id
                        else None,
                        route="/spending?tab=budgets",
                    ),
                )
            )
        return lines

    async def _recurring_due_lines(self, workspace_id: int, now: datetime) -> list[BriefingLine]:
        today = now.date()
        tomorrow = today + timedelta(days=1)
        lines: list[BriefingLine] = []

        due_transactions = await self.recurring_transaction_service.get_due_between(
            workspace_id, today, tomorrow
        )
        for rule, category_name in due_transactions:
            due_word = "today" if rule.next_due_date == today else "tomorrow"
            label = rule.description or category_name
            lines.append(
                BriefingLine(
                    severity="info",
                    text=f"Recurring {rule.type} due {due_word}: {label} ({rule.amount:.2f})",
                    source=BriefingSource(
                        entity_type="recurring_transaction",
                        entity_public_id=str(rule.public_id),
                        route="/spending?tab=recurring",
                    ),
                )
            )

        due_todo_rules = await self.todo_service.get_recurring_rules_due_between(
            workspace_id, today, tomorrow
        )
        for rule in due_todo_rules:
            due_word = "today" if rule.next_due_date == today else "tomorrow"
            lines.append(
                BriefingLine(
                    severity="info",
                    text=f'Recurring todo due {due_word}: "{rule.title}"',
                    source=BriefingSource(
                        entity_type="recurring_todo_rule",
                        entity_public_id=str(rule.public_id),
                        route="/todo?tab=recurring",
                    ),
                )
            )
        return lines

    async def _net_worth_lines(self, workspace_id: int) -> list[BriefingLine]:
        performance = await self.investing_performance_service.summary(workspace_id)
        if performance.daily_change is None:
            return []
        degraded = performance.valuation_status in ("partial", "conversion_required")
        sign = "+" if performance.daily_change >= 0 else ""
        pct_text = (
            f" ({sign}{performance.daily_change_pct:.2f}%)"
            if performance.daily_change_pct is not None
            else ""
        )
        text = f"Portfolio {sign}{performance.daily_change:.2f}{pct_text} today"
        if degraded:
            text += f" — valuation {performance.valuation_status}"
        return [
            BriefingLine(
                severity="warning" if degraded else "info",
                text=text,
                source=BriefingSource(route="/investing"),
            )
        ]

    async def _pending_review_lines(self, workspace_id: int) -> list[BriefingLine]:
        batches, total = await self.import_repo.list_pending_review(workspace_id, limit=1)
        if total <= 0:
            return []
        plural = "s" if total != 1 else ""
        top = batches[0] if batches else None
        return [
            BriefingLine(
                severity="warning",
                text=f"{total} import{plural} awaiting commit",
                source=BriefingSource(
                    entity_type="import_batch",
                    entity_public_id=str(top.public_id) if top else None,
                    route="/imports",
                ),
            )
        ]

    async def _weekly_summary_lines(self, workspace_id: int, now: datetime) -> list[BriefingLine]:
        latest = await self.weekly_summary_repo.latest(workspace_id)
        if latest is None:
            return []
        # Already opened it? Drop the line — reading is the natural dismissal
        # (spec-080), instead of nagging for the full freshness window.
        if latest.read_at is not None:
            return []
        age = now - latest.generated_at
        if age > timedelta(hours=_BRIEFING_WEEKLY_SUMMARY_FRESH_HOURS):
            return []
        return [
            BriefingLine(
                severity="info",
                text=f"Weekly summary for week of {latest.week_start.isoformat()} is ready",
                source=BriefingSource(
                    entity_type="weekly_summary",
                    entity_public_id=str(latest.public_id),
                    route="/summaries",
                ),
            )
        ]

    async def _fresh_insight_lines(
        self, workspace_id: int, user_id: int, now: datetime
    ) -> list[BriefingLine]:
        since = now - timedelta(hours=_BRIEFING_FRESH_INSIGHT_HOURS)
        notifications = await self.notification_service.list_recent_unread(
            workspace_id, user_id, "insight", since, limit=5
        )
        lines = []
        for notification in notifications:
            severity = (
                notification.severity
                if notification.severity
                in (
                    "critical",
                    "warning",
                    "info",
                )
                else "info"
            )
            route = _INSIGHT_ENTITY_ROUTES.get(notification.entity_type or "", "/notifications")
            lines.append(
                BriefingLine(
                    severity=severity,
                    text=notification.title,
                    source=BriefingSource(
                        entity_type=notification.entity_type,
                        entity_public_id=str(notification.entity_public_id)
                        if notification.entity_public_id
                        else None,
                        route=route,
                    ),
                )
            )
        return lines


# ---------------------------------------------------------------------------
# Budget Guardrails Workflow
# ---------------------------------------------------------------------------


def _snapshot_todo(todo: Todo) -> dict:
    data = snapshot_columns(todo, _TODO_AUDIT_FIELDS)
    # Convert date field to ISO format for JSON serialization
    if data.get("due_date") is not None:
        data["due_date"] = data["due_date"].isoformat()
    return data


async def evaluate_workspace_budget_guardrails(session: AsyncSession, workspace: Workspace) -> None:
    """
    Evaluate budget guardrail thresholds for a single workspace.

    For each budget in the current month:
    - If spend >= WARNING_THRESHOLD: upsert a system todo (idempotent).
    - If spend >= CRITICAL_THRESHOLD: escalate to critical priority.
    - If spend drops back below threshold: auto-resolve the system todo.
    - All actions are written to the audit log within the same transaction.
    """
    logger.info("evaluating_budget_guardrails", workspace_id=workspace.id)

    # 1. Resolve workspace owner for assigning system todos
    members_res = await session.execute(
        select(WorkspaceMembership.user_id)
        .where(WorkspaceMembership.workspace_id == workspace.id)
        .order_by(
            (WorkspaceMembership.role == "owner").desc(), WorkspaceMembership.created_at.asc()
        )
        .limit(1)
    )
    user_id = members_res.scalar()
    if not user_id:
        logger.warning("no_members_in_workspace", workspace_id=workspace.id)
        return

    # 2. Determine current month date bounds
    today = datetime.now(UTC).date()
    month_start = today.replace(day=1)
    if month_start.month == 12:
        next_month_start = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month_start = month_start.replace(month=month_start.month + 1)

    current_month_dt = datetime(month_start.year, month_start.month, 1, tzinfo=UTC)
    next_month_dt = datetime(next_month_start.year, next_month_start.month, 1, tzinfo=UTC)

    # 3. Fetch budgets for current month
    budgets_res = await session.execute(
        select(SpendingBudget).where(
            SpendingBudget.workspace_id == workspace.id,
            SpendingBudget.start_month <= month_start,
            or_(
                SpendingBudget.end_month.is_(None),
                SpendingBudget.end_month >= month_start,
            ),
        )
    )
    budgets = budgets_res.scalars().all()
    if not budgets:
        logger.info("no_budgets_for_workspace", workspace_id=workspace.id, month=str(month_start))
        return

    # 4. Build category lookup map
    cats_res = await session.execute(
        select(SpendingCategory).where(SpendingCategory.workspace_id == workspace.id)
    )
    cats = {c.id: c for c in cats_res.scalars().all()}

    # 5. Aggregate expense spend per category for the current month in SQL
    tx_sum_query = (
        select(SpendingTransaction.category_id, func.sum(SpendingTransaction.amount))
        .where(
            SpendingTransaction.workspace_id == workspace.id,
            SpendingTransaction.type == TransactionType.expense,
            SpendingTransaction.occurred_at >= current_month_dt,
            SpendingTransaction.occurred_at < next_month_dt,
        )
        .group_by(SpendingTransaction.category_id)
    )
    tx_sum_res = await session.execute(tx_sum_query)
    spend_per_category = {cat_id: float(total or 0.0) for cat_id, total in tx_sum_res.all()}

    # 6. Pre-fetch all relevant system todos for this workspace to avoid N+1 queries
    todos_res = await session.execute(
        select(Todo).where(
            Todo.workspace_id == workspace.id,
            Todo.system_key.like("budget:guardrail:%"),
        )
    )
    todos_map = {todo.system_key: todo for todo in todos_res.scalars().all()}

    audit_logger = AuditLogger(session)
    todo_service = TodoService(TodoRepository(session))

    # 7. Evaluate each budget against thresholds
    for budget in budgets:
        category = cats.get(budget.category_id)
        if not category:
            continue

        budget_amount = float(budget.amount)
        if budget_amount <= 0:
            continue

        actual_spend = spend_per_category.get(budget.category_id, 0.0)
        ratio = actual_spend / budget_amount
        system_key = f"budget:guardrail:{budget.category_id}"

        todo = todos_map.get(system_key)

        is_warning = ratio >= settings.BUDGET_WARNING_THRESHOLD
        is_critical = ratio >= settings.BUDGET_CRITICAL_THRESHOLD

        if is_warning:
            severity = "critical" if is_critical else "warning"
            title = f"[Budget] {severity.capitalize()}: {category.name}"
            desc = (
                f"Your spend for {category.name} is {actual_spend:.2f} "
                f"of your {budget_amount:.2f} budget ({ratio * 100:.1f}%)."
            )
            priority = PriorityEnum.high if is_critical else PriorityEnum.medium

            todo, change = await todo_service.ensure_system_task(
                workspace_id=workspace.id,  # type: ignore[arg-type]
                user_id=user_id,  # type: ignore[arg-type]
                system_key=system_key,
                title=title,
                description=desc,
                priority=priority,
                existing_todo=todo,
                audit_logger=audit_logger,
                audit_module="application",
                audit_action="budget_guardrail_triggered",
            )
            todos_map[system_key] = todo
            if change == "updated":
                logger.info(
                    "budget_guardrail_todo_updated",
                    workspace_id=workspace.id,
                    category=category.name,
                    severity=severity,
                    ratio=f"{ratio:.1%}",
                )
            elif change == "created":
                logger.info(
                    "budget_guardrail_todo_created",
                    workspace_id=workspace.id,
                    category=category.name,
                    severity=severity,
                    ratio=f"{ratio:.1%}",
                )
        else:
            # Spend dropped below threshold — auto-resolve the guardrail todo
            if todo and not todo.completed:
                before_snap = _snapshot_todo(todo)
                todo.completed = True
                todo.updated_at = datetime.now(UTC)
                session.add(todo)
                await session.flush()
                after_snap = _snapshot_todo(todo)
                changed_fields = [k for k in before_snap if before_snap[k] != after_snap[k]]
                await audit_logger.log(
                    workspace_id=workspace.id,
                    actor_id=user_id,
                    action="budget_guardrail_triggered",
                    module="application",
                    entity_type="todo",
                    entity_id=todo.id,  # type: ignore[arg-type]
                    details={
                        "entity_public_id": str(todo.public_id),
                        "before": before_snap,
                        "after": after_snap,
                        "changed_fields": changed_fields,
                    },
                )
                logger.info(
                    "budget_guardrail_todo_auto_resolved",
                    workspace_id=workspace.id,
                    category=category.name,
                )


async def evaluate_workspace_kpi_breaches(session: AsyncSession, workspace: Workspace) -> None:
    """Evaluate every active custom financial KPI for a workspace and notify
    on target breaches (spec-077).

    Unlike budget guardrails (which drive a system Todo, resolved generically
    by ``todo_reminder_job``), spec-077 names ``Notification(category="kpi")``
    directly — mirrors ``app.application.insights``'s detector shape:
    dedup via an existence check on ``Notification.entity_public_id`` scoped
    to the KPI's current window, rather than a stored breach-state row, since
    KPI values are always recomputed and never persisted (spec-077: "no new
    stored aggregates")."""
    if workspace.id is None:
        return

    members_res = await session.execute(
        select(WorkspaceMembership.user_id)
        .where(WorkspaceMembership.workspace_id == workspace.id)
        .order_by(
            (WorkspaceMembership.role == "owner").desc(), WorkspaceMembership.created_at.asc()
        )
        .limit(1)
    )
    user_id = members_res.scalar()
    if not user_id:
        logger.warning("kpi_guardrails_no_members", workspace_id=workspace.id)
        return

    kpi_service = KpiService(
        KpiRepository(session),
        CategoryRepository(session),
        CategoryGroupRepository(session),
        AccountRepository(session),
        SpendingTransactionRepository(session),
    )
    notification_service = NotificationService(NotificationRepository(session))

    for kpi, current_value, is_breached in await kpi_service.evaluate_active_kpis(workspace.id):
        if not is_breached:
            continue

        _, _, window_start, _ = kpi_service._window_bounds(  # noqa: SLF001 — same-module evaluator
            kpi.evaluation_window, datetime.now(UTC).date()
        )
        window_start_dt = datetime.combine(window_start, datetime.min.time(), tzinfo=UTC)

        already_notified = await session.execute(
            select(Notification.id).where(
                Notification.workspace_id == workspace.id,
                Notification.user_id == user_id,
                Notification.category == "kpi",
                Notification.entity_type == "financial_kpi",
                Notification.entity_public_id == kpi.public_id,
                Notification.created_at >= window_start_dt,
            )
        )
        if already_notified.scalar() is not None:
            continue

        direction_text = "at most" if kpi.target_direction == "lte" else "at least"
        await notification_service.notify(
            workspace_id=workspace.id,
            user_id=user_id,
            category="kpi",
            severity="warning",
            title=f"KPI breached: {kpi.name}",
            body=(
                f"{kpi.name} is {current_value:.2f} {kpi.currency_code} this "
                f"{kpi.evaluation_window.replace('_', ' ')}, target is {direction_text} "
                f"{kpi.target_value:.2f} {kpi.currency_code}."
            ),
            module="spending",
            entity_type="financial_kpi",
            entity_public_id=kpi.public_id,
        )
        logger.info(
            "kpi_breach_notified",
            workspace_id=workspace.id,
            kpi_id=str(kpi.public_id),
            current_value=str(current_value),
        )


# ---------------------------------------------------------------------------
# Recurring Transactions Workflow (Spec 013)
# ---------------------------------------------------------------------------


async def process_workspace_recurring_transactions(
    session: AsyncSession, workspace: Workspace
) -> int:
    """
    Generate spending transactions for all due recurring rules in a workspace.

    For each active recurring transaction whose next_due_date <= today:
    - Generate a SpendingTransaction (with recurring_transaction_id linked).
    - Advance next_due_date by frequency * interval.
    - Repeat until next_due_date > today (catch-up mode).
    - Cap catch-up at settings.RECURRING_TXN_CATCHUP_LIMIT_DAYS to prevent runaway.
    - If the new next_due_date > end_date, deactivate the rule.
    - Emit an audit event per generated transaction.

    Returns the total number of transactions generated across all recurrences.
    """
    today = datetime.now(UTC).date()
    catchup_limit_days = settings.RECURRING_TXN_CATCHUP_LIMIT_DAYS
    catchup_boundary = today - timedelta(days=catchup_limit_days)

    logger.info("processing_recurring_transactions", workspace_id=workspace.id, today=str(today))

    # Fetch the workspace owner for the audit actor_id
    members_res = await session.execute(
        select(WorkspaceMembership.user_id)
        .where(WorkspaceMembership.workspace_id == workspace.id)
        .order_by(
            (WorkspaceMembership.role == "owner").desc(), WorkspaceMembership.created_at.asc()
        )
        .limit(1)
    )
    user_id = members_res.scalar()
    if not user_id:
        logger.warning("no_members_in_workspace_recurring", workspace_id=workspace.id)
        return 0

    # Fetch all due recurring rules for this workspace
    due_recurrences_res = await session.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.workspace_id == workspace.id,
            RecurringTransaction.is_active == True,  # noqa: E712
            RecurringTransaction.next_due_date <= today,
        )
    )
    due_recurrences = due_recurrences_res.scalars().all()

    if not due_recurrences:
        logger.info("no_due_recurrences", workspace_id=workspace.id)
        return 0

    audit_logger = AuditLogger(session)
    total_generated = 0
    catchup_warned = False
    generated_txs: list[tuple[SpendingTransaction, RecurringTransaction]] = []

    for recurrence in due_recurrences:
        # Safety: if due date predates boundary, fast-forward preserving cadence alignment.
        if recurrence.next_due_date < catchup_boundary:
            # The scheduler was down for too long; log warning and fast-forward
            if not catchup_warned:
                logger.warning(
                    "recurring_catchup_cap_applied",
                    workspace_id=workspace.id,
                    recurrence_id=recurrence.id,
                    days_overdue=(today - recurrence.next_due_date).days,
                    cap_days=catchup_limit_days,
                )
                catchup_warned = True
            while recurrence.next_due_date < catchup_boundary:
                next_due = advance_due_date(
                    recurrence.next_due_date,
                    recurrence.frequency,
                    recurrence.interval,
                    anchor_day=recurrence.anchor_date.day,
                    monthly_mode=recurrence.monthly_mode,
                    by_weekday=recurrence.by_weekday,
                    by_ordinal=recurrence.by_ordinal,
                )
                if next_due <= recurrence.next_due_date:
                    logger.error(
                        "recurring_catchup_advance_failed",
                        workspace_id=workspace.id,
                        recurrence_id=recurrence.id,
                        prev_due=str(recurrence.next_due_date),
                        next_due=str(next_due),
                    )
                    break
                recurrence.next_due_date = next_due

        generated_count = 0

        # Inner catch-up loop — generate one transaction per missed period
        while recurrence.next_due_date <= today:
            # Respect end_date: skip if already past it
            if recurrence.end_date and recurrence.next_due_date > recurrence.end_date:
                recurrence.is_active = False
                break

            # Generate the spending transaction
            occurred_at = datetime.combine(recurrence.next_due_date, datetime.min.time()).replace(
                tzinfo=UTC
            )
            tx = SpendingTransaction(
                workspace_id=workspace.id,
                user_id=user_id,
                category_id=recurrence.category_id,
                amount=recurrence.amount,
                type=recurrence.type,
                description=recurrence.description,
                occurred_at=occurred_at,
                recurring_transaction_id=recurrence.id,
            )
            session.add(tx)
            generated_txs.append((tx, recurrence))

            # Advance to next occurrence
            prev_due = recurrence.next_due_date
            recurrence.next_due_date = advance_due_date(
                recurrence.next_due_date,
                recurrence.frequency,
                recurrence.interval,
                anchor_day=recurrence.anchor_date.day,
                monthly_mode=recurrence.monthly_mode,
                by_weekday=recurrence.by_weekday,
                by_ordinal=recurrence.by_ordinal,
            )
            if recurrence.next_due_date <= prev_due:
                logger.error(
                    "recurring_transaction_advance_failed",
                    workspace_id=workspace.id,
                    recurrence_id=recurrence.id,
                    prev_due=str(prev_due),
                    next_due=str(recurrence.next_due_date),
                )
                break
            recurrence.last_generated_at = datetime.now(UTC)
            generated_count += 1
            total_generated += 1

            # Deactivate if past end_date after advancing
            if recurrence.end_date and recurrence.next_due_date > recurrence.end_date:
                recurrence.is_active = False
                logger.info(
                    "recurring_transaction_exhausted",
                    workspace_id=workspace.id,
                    recurrence_id=recurrence.id,
                )
                break

        session.add(recurrence)

        if generated_count > 0:
            logger.info(
                "recurring_transactions_generated",
                workspace_id=workspace.id,
                recurrence_id=recurrence.id,
                count=generated_count,
            )
        elif generated_count == 0 and not recurrence.is_active:
            # Was already past end_date when we picked it up
            session.add(recurrence)

    await session.flush()
    for tx, recurrence in generated_txs:
        # Audit generated transactions after a single batch flush so tx IDs exist.
        after_snap = {
            "amount": str(tx.amount),
            "type": tx.type,
            "occurred_at": tx.occurred_at.isoformat(),
            "description": tx.description,
            "recurring_transaction_id": recurrence.id,
            "recurring_public_id": str(recurrence.public_id),
        }
        await audit_logger.log(
            workspace_id=workspace.id,
            actor_id=user_id,
            action="recurring_transaction_generated",
            module="application",
            entity_type="spending_transaction",
            entity_id=tx.id,  # type: ignore[arg-type]
            details={
                "entity_public_id": str(tx.public_id),
                "before": None,
                "after": after_snap,
                "changed_fields": list(after_snap.keys()),
            },
        )
    return total_generated


async def process_workspace_recurring_todos(session: AsyncSession, workspace: Workspace) -> int:
    """Generate due todos from recurring todo rules for one workspace."""
    today = datetime.now(UTC).date()
    catchup_limit_days = settings.RECURRING_TODO_CATCHUP_LIMIT_DAYS
    catchup_boundary = today - timedelta(days=catchup_limit_days)
    logger.info("processing_recurring_todos", workspace_id=workspace.id, today=str(today))

    members_res = await session.execute(
        select(WorkspaceMembership.user_id)
        .where(WorkspaceMembership.workspace_id == workspace.id)
        .order_by(
            (WorkspaceMembership.role == "owner").desc(), WorkspaceMembership.created_at.asc()
        )
        .limit(1)
    )
    user_id = members_res.scalar()
    if not user_id:
        logger.warning("no_members_in_workspace_recurring_todos", workspace_id=workspace.id)
        return 0

    rules_res = await session.execute(
        select(RecurringTodoRule).where(
            RecurringTodoRule.workspace_id == workspace.id,
            RecurringTodoRule.is_active == True,  # noqa: E712
            RecurringTodoRule.next_due_date <= today,
        )
    )
    rules = rules_res.scalars().all()
    if not rules:
        return 0

    generated = 0
    for rule in rules:
        if rule.next_due_date < catchup_boundary:
            while rule.next_due_date < catchup_boundary:
                next_due = advance_due_date(
                    rule.next_due_date,
                    rule.frequency,
                    rule.interval,
                    anchor_day=rule.anchor_date.day,
                    monthly_mode=rule.monthly_mode,
                    by_weekday=rule.by_weekday,
                    by_ordinal=rule.by_ordinal,
                )
                if next_due <= rule.next_due_date:
                    logger.error(
                        "recurring_todo_catchup_advance_failed",
                        workspace_id=workspace.id,
                        rule_id=rule.id,
                        prev_due=str(rule.next_due_date),
                        next_due=str(next_due),
                    )
                    break
                rule.next_due_date = next_due
        while rule.is_active and rule.next_due_date <= today:
            if rule.end_date and rule.next_due_date > rule.end_date:
                rule.is_active = False
                break
            try:
                rule_timezone = ZoneInfo(rule.timezone or "UTC")
            except ZoneInfoNotFoundError:
                logger.warning(
                    "recurring_todo_invalid_timezone",
                    workspace_id=workspace.id,
                    rule_id=rule.id,
                    timezone=rule.timezone,
                )
                rule_timezone = UTC
            due_dt = datetime.combine(
                rule.next_due_date,
                rule.due_time or time.min,
                tzinfo=rule_timezone,
            ).astimezone(UTC)
            session.add(
                Todo(
                    workspace_id=workspace.id,
                    user_id=user_id,
                    title=rule.title,
                    description=rule.description,
                    due_date=due_dt,
                    priority=rule.priority,
                    completed=False,
                )
            )
            generated += 1
            prev_due = rule.next_due_date
            rule.next_due_date = advance_due_date(
                rule.next_due_date,
                rule.frequency,
                rule.interval,
                anchor_day=rule.anchor_date.day,
                monthly_mode=rule.monthly_mode,
                by_weekday=rule.by_weekday,
                by_ordinal=rule.by_ordinal,
            )
            if rule.next_due_date <= prev_due:
                logger.error(
                    "recurring_todo_advance_failed",
                    workspace_id=workspace.id,
                    rule_id=rule.id,
                    prev_due=str(prev_due),
                    next_due=str(rule.next_due_date),
                )
                break
            rule.last_generated_at = datetime.now(UTC)
            if rule.end_date and rule.next_due_date > rule.end_date:
                rule.is_active = False
            session.add(rule)

    await session.flush()
    return generated


async def ingest_fx_rates(session: AsyncSession) -> None:
    """
    Ingest daily FX rates from ExchangeRate-API (https://www.exchangerate-api.com).
    Uses USD as the base currency.
    If the API key is not configured or the call fails, raises an exception cleanly.
    """
    if not settings.EXCHANGERATE_API_KEY:
        raise ValueError("EXCHANGERATE_API_KEY environment variable is not configured.")

    logger.info("ingesting_fx_rates_start", base_currency="USD")

    url = f"https://v6.exchangerate-api.com/v6/{settings.EXCHANGERATE_API_KEY}/latest/USD"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    if not isinstance(data, dict) or data.get("result") != "success":
        error_type = (
            data.get("error-type", "unknown")
            if isinstance(data, dict)
            else "invalid response format"
        )
        raise ValueError(f"ExchangeRate-API request failed: {error_type}")

    conversion_rates = data.get("conversion_rates")
    if not isinstance(conversion_rates, dict):
        raise ValueError("ExchangeRate-API response is missing conversion_rates")

    for code in ["USD", "GBP", "INR"]:
        if code not in conversion_rates:
            raise KeyError(
                f"Expected currency code '{code}' missing from ExchangeRate-API conversion rates."
            )

    # Base rates relative to USD
    usd_to_gbp = Decimal(str(conversion_rates["GBP"]))
    usd_to_inr = Decimal(str(conversion_rates["INR"]))
    if usd_to_gbp <= 0 or usd_to_inr <= 0:
        raise ValueError("ExchangeRate-API returned non-positive conversion rates.")

    # Derived rates
    raw_rates = [
        # USD -> GBP
        ("USD", "GBP", usd_to_gbp),
        # GBP -> USD
        ("GBP", "USD", Decimal("1.0") / usd_to_gbp),
        # USD -> INR
        ("USD", "INR", usd_to_inr),
        # INR -> USD
        ("INR", "USD", Decimal("1.0") / usd_to_inr),
        # GBP -> INR
        ("GBP", "INR", usd_to_inr / usd_to_gbp),
        # INR -> GBP
        ("INR", "GBP", usd_to_gbp / usd_to_inr),
    ]

    rates_to_upsert = []
    for base, quote, rate in raw_rates:
        quantized_rate = rate.quantize(Decimal("1.0000000000"))
        if quantized_rate <= 0:
            raise ValueError(
                f"Derived rate for {base}/{quote} quantized to non-positive value: {quantized_rate}"
            )
        rates_to_upsert.append((base, quote, quantized_rate))

    currency_repo = CurrencyRepository(session)
    fx_repo = FxRateRepository(session)
    fx_service = FxRateService(fx_repo, currency_repo)

    now = datetime.now(UTC)
    as_of_unix = data.get("time_last_update_unix")
    try:
        as_of = datetime.fromtimestamp(float(as_of_unix), UTC) if as_of_unix is not None else now
    except (ValueError, TypeError):
        as_of = now

    for base, quote, rate in rates_to_upsert:
        payload = FxRateUpsert(
            base_currency_code=base,
            quote_currency_code=quote,
            rate=rate,
            as_of=as_of,
            fetched_at=now,
            source="exchangerate-api",
        )
        await fx_service.upsert(payload)

    logger.info("ingesting_fx_rates_success", count=len(rates_to_upsert))


async def cleanup_expired_exports(session: AsyncSession) -> int:
    """Identify and clean up expired exports based on EXPORT_TTL_DAYS."""

    if not settings.EXPORT_CLEANUP_ENABLED:
        logger.info("export_cleanup_disabled")
        return 0

    cutoff = datetime.now(UTC) - timedelta(days=settings.EXPORT_TTL_DAYS)

    # Query exports to clean up: ready/failed exports older than EXPORT_TTL_DAYS,
    # or pending exports older than EXPORT_TTL_DAYS
    query = select(ExportRecord).where(
        (
            ExportRecord.status.in_([ExportStatus.ready, ExportStatus.failed])
            & (ExportRecord.completed_at < cutoff)
        )
        | ((ExportRecord.status == ExportStatus.pending) & (ExportRecord.created_at < cutoff))
    )
    result = await session.execute(query)
    expired_records = result.scalars().all()

    if not expired_records:
        return 0

    repo = ExportRepository(session)
    service = ExportService(repo)

    cleaned_count = 0
    for record in expired_records:
        try:
            if settings.EXPORT_CLEANUP_DELETE_RECORDS:
                await service.delete_export(record.workspace_id, record.public_id)
            else:
                if settings.EXPORT_CLEANUP_DELETE_FILES and record.storage_key:
                    storage_key = record.storage_key
                    if storage_key.startswith("local://"):
                        filepath = Path(storage_key[8:])
                        try:
                            if await asyncio.to_thread(filepath.exists):
                                await asyncio.to_thread(filepath.unlink)
                        except Exception as e:
                            logger.warning(
                                "failed_to_delete_local_file_cleanup",
                                error=str(e),
                                storage_key=storage_key,
                            )
                    elif storage_key.startswith("s3://"):
                        parts = storage_key[5:].split("/", 1)
                        if len(parts) == 2:
                            bucket, key = parts
                            try:
                                client, _ = service._get_s3_client()
                                await asyncio.to_thread(
                                    client.delete_object, Bucket=bucket, Key=key
                                )
                            except Exception as e:
                                logger.warning(
                                    "failed_to_delete_s3_object_cleanup",
                                    error=str(e),
                                    storage_key=storage_key,
                                )
                    elif not storage_key.startswith("db://"):
                        filepath = Path(storage_key)
                        try:
                            if filepath.is_absolute() and await asyncio.to_thread(filepath.exists):
                                await asyncio.to_thread(filepath.unlink)
                        except Exception as e:
                            logger.warning(
                                "failed_to_delete_local_file_cleanup",
                                error=str(e),
                                storage_key=storage_key,
                            )

                record.status = ExportStatus.expired
                record.artifact_blob = None
                record.storage_key = None
                await repo.save(record, refresh=False)

            cleaned_count += 1
        except Exception as e:
            logger.error(
                "failed_to_clean_expired_export", export_id=record.id, error=str(e), exc_info=True
            )

    await session.flush()
    return cleaned_count


async def cleanup_expired_sessions(session: AsyncSession) -> int:
    """Purge expired and revoked auth sessions."""
    repo = AuthSessionRepository(session)
    return await repo.delete_expired_and_revoked_sessions()


async def cleanup_import_previews(session: AsyncSession) -> int:
    """Delete import preview rows older than IMPORT_PREVIEW_TTL_HOURS."""
    cutoff = datetime.now(UTC) - timedelta(hours=settings.IMPORT_PREVIEW_TTL_HOURS)
    subquery = select(ImportBatch.id).where(ImportBatch.created_at < cutoff)
    statement = delete(ImportPreviewRow).where(ImportPreviewRow.import_batch_id.in_(subquery))
    result = await session.execute(statement)
    return result.rowcount or 0


async def process_workspace_todo_reminders(
    session: AsyncSession, workspace: Workspace, window_end: datetime
) -> int:
    """Create a due-reminder Notification for each incomplete todo whose
    due_date falls within the look-ahead window and hasn't been reminded yet
    (spec-052). Push delivery then happens for free via NotificationService.notify's
    existing enqueue step. Returns the number of reminders created."""
    if workspace.id is None:
        return 0

    todo_repo = TodoRepository(session)
    todos = await todo_repo.get_due_for_reminder(workspace.id, window_end)
    if not todos:
        return 0

    notification_repo = NotificationRepository(session)
    notification_service = NotificationService(notification_repo)

    # Multiple due todos commonly share a user; batch-fetch every distinct
    # user's preference/subscription status in two queries total instead of
    # one (or two) per todo (N+1).
    distinct_user_ids = {todo.user_id for todo in todos}
    preference_cache = await notification_repo.get_preferences_for_users(
        workspace.id, distinct_user_ids, "todo_reminder"
    )
    users_needing_push = {
        user_id
        for user_id in distinct_user_ids
        if (pref := preference_cache.get(user_id)) and pref.channel_push
    }
    users_with_push = await notification_repo.users_with_active_push_subscription(
        workspace.id, users_needing_push
    )
    subscription_cache = {user_id: user_id in users_with_push for user_id in users_needing_push}

    reminded = 0
    for todo in todos:
        notification = await notification_service.notify(
            workspace_id=workspace.id,
            user_id=todo.user_id,
            category="todo_reminder",
            severity="info",
            title=f"Reminder: {todo.title}",
            body=todo.description or None,
            module="todo",
            entity_type="todo",
            entity_public_id=todo.public_id,
            preference=preference_cache.get(todo.user_id),
            has_push_subscription=subscription_cache.get(todo.user_id),
        )
        if notification is not None:
            todo.reminded_at = datetime.now(UTC)
            session.add(todo)
            reminded += 1
    if reminded:
        await session.flush()
    return reminded


async def process_workspace_medication_reminders(
    session: AsyncSession, workspace: Workspace, now: datetime, window_end: datetime
) -> int:
    """Create exactly one due-reminder Notification per dose slot for active
    medications with reminders_enabled (spec-069 §C) — clone of
    process_workspace_todo_reminders. Idempotent via
    Medication.last_reminded_slot (the reminded_at pattern, keyed to the
    slot datetime rather than a boolean)."""
    if workspace.id is None:
        return 0

    medication_repo = MedicationRepository(session)
    medications = await medication_repo.get_active_with_reminders(workspace.id)
    if not medications:
        return 0

    notification_repo = NotificationRepository(session)
    notification_service = NotificationService(notification_repo)

    distinct_user_ids = {med.user_id for med in medications}
    preference_cache = await notification_repo.get_preferences_for_users(
        workspace.id, distinct_user_ids, "medication_reminder"
    )
    users_needing_push = {
        user_id
        for user_id in distinct_user_ids
        if (pref := preference_cache.get(user_id)) and pref.channel_push
    }
    users_with_push = await notification_repo.users_with_active_push_subscription(
        workspace.id, users_needing_push
    )
    subscription_cache = {user_id: user_id in users_with_push for user_id in users_needing_push}

    reminded = 0
    for med in medications:
        due_slots = sorted(
            slot
            for slot in get_dose_slots_in_window(med, now, window_end)
            if med.last_reminded_slot is None or slot > med.last_reminded_slot
        )
        try:
            tz = ZoneInfo(med.timezone)
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            tz = UTC
        for slot in due_slots:
            local_time = slot.astimezone(tz).strftime("%H:%M")
            body = f"{med.dose_text} — {local_time}" if med.dose_text else local_time
            notification = await notification_service.notify(
                workspace_id=workspace.id,
                user_id=med.user_id,
                category="medication_reminder",
                severity="info",
                title=med.name,
                body=body,
                module="health",
                entity_type="medication",
                entity_public_id=med.public_id,
                preference=preference_cache.get(med.user_id),
                has_push_subscription=subscription_cache.get(med.user_id),
            )
            if notification is not None:
                med.last_reminded_slot = slot
                session.add(med)
                reminded += 1
    if reminded:
        await session.flush()
    return reminded


async def deliver_pending_push_notifications(session: AsyncSession, limit: int = 100) -> dict:
    """Drain the pending push-delivery queue (spec-052): one Notification can
    fan out to every active subscription of its user; the single delivery
    row's status folds all per-subscription outcomes together (`sent` if any
    endpoint accepted, `failed` with detail if all failed). A 404/410 from a
    push service means that subscription no longer exists — deactivate it and
    continue, never fail the whole run over one dead endpoint."""
    notification_repo = NotificationRepository(session)
    subscription_repo = PushSubscriptionRepository(session)

    pending = await notification_repo.list_pending_push_deliveries(limit)
    sent_count = 0
    failed_count = 0

    for delivery, notification in pending:
        try:
            subscriptions = await subscription_repo.list_active_for_user(
                notification.workspace_id, notification.user_id
            )
            if not subscriptions:
                await notification_repo.mark_delivery(
                    delivery, "failed", "no active push subscriptions"
                )
                failed_count += 1
                continue

            payload = {
                "title": notification.title,
                "body": notification.body,
                "entity_type": notification.entity_type,
                "entity_public_id": str(notification.entity_public_id)
                if notification.entity_public_id
                else None,
            }

            any_success = False
            last_error: str | None = None
            for subscription in subscriptions:
                result = await asyncio.to_thread(
                    send_web_push,
                    subscription.endpoint,
                    subscription.p256dh,
                    subscription.auth,
                    payload,
                )
                if result.success:
                    any_success = True
                    await subscription_repo.mark_success(subscription)
                else:
                    last_error = result.error_detail
                    await subscription_repo.mark_failure(subscription, deactivate=result.gone)

            if any_success:
                await notification_repo.mark_delivery(delivery, "sent")
                sent_count += 1
            else:
                await notification_repo.mark_delivery(delivery, "failed", last_error)
                failed_count += 1
        except Exception:
            logger.error(
                "push_delivery_row_failed",
                notification_id=notification.id,
                delivery_id=delivery.id,
                exc_info=True,
            )
            await notification_repo.mark_delivery(delivery, "failed", "internal error")
            failed_count += 1

    if pending:
        await session.flush()
    return {"sent": sent_count, "failed": failed_count}


async def deliver_pending_email_notifications(session: AsyncSession, limit: int = 100) -> dict:
    """Drain the pending email-delivery queue (spec-081), mirroring
    ``deliver_pending_push_notifications``. ``limit`` doubles as the
    defensive per-run cap on Resend free-tier volume (100/day) — the caller
    passes ``settings.EMAIL_DELIVERY_BATCH_CAP``, not the push default."""
    notification_repo = NotificationRepository(session)
    user_repo = UserRepository(session)

    pending = await notification_repo.list_pending_email_deliveries(limit)
    sent_count = 0
    failed_count = 0
    skipped_count = 0

    if not pending:
        return {"sent": sent_count, "failed": failed_count, "skipped": skipped_count}

    async with httpx.AsyncClient() as client:
        for delivery, notification in pending:
            try:
                user = await user_repo.get_by_id(notification.user_id)
                if not user:
                    await notification_repo.mark_delivery(delivery, "failed", "user not found")
                    failed_count += 1
                    continue

                html = f"<p><strong>{notification.title}</strong></p>"
                if notification.body:
                    html += f"<p>{notification.body}</p>"

                result = await send_email(user.email, notification.title, html, client=client)
                if result.skipped:
                    await notification_repo.mark_delivery(delivery, "skipped")
                    skipped_count += 1
                elif result.success:
                    await notification_repo.mark_delivery(delivery, "sent")
                    sent_count += 1
                else:
                    await notification_repo.mark_delivery(delivery, "failed", result.error_detail)
                    failed_count += 1
            except Exception:
                logger.error(
                    "email_delivery_row_failed",
                    notification_id=notification.id,
                    delivery_id=delivery.id,
                    exc_info=True,
                )
                await notification_repo.mark_delivery(delivery, "failed", "internal error")
                failed_count += 1

    await session.flush()
    return {"sent": sent_count, "failed": failed_count, "skipped": skipped_count}
