import asyncio
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.repository import AuthSessionRepository
from app.auth.schemas import UserCreate
from app.auth.service import AuthService
from app.config import settings
from app.core.audit import AuditLogger
from app.dashboard.schemas import (
    DashboardSummary,
    InvestingSummary,
    SpendingSummary,
    SystemSummary,
    TodosSummary,
)
from app.exports.models import ExportRecord, ExportStatus
from app.exports.repository import ExportRepository
from app.exports.service import ExportService
from app.finance.repository import CurrencyRepository, FxRateRepository
from app.finance.schemas import FxRateUpsert
from app.finance.service import FxRateService
from app.imports.models import ImportBatch, ImportPreviewRow
from app.investing.service import PerformanceService
from app.platform.models import Workspace, WorkspaceMembership
from app.platform.service import WorkspaceService
from app.spending.models import (
    RecurringTransaction,
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
    TransactionType,
)
from app.spending.service import (
    BudgetService,
    CategoryService,
    TransactionService,
    _advance_due_date,
)
from app.todo.models import PriorityEnum, RecurringTodoRule, Todo
from app.todo.repository import TodoRepository
from app.todo.service import TodoService

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
            month_budget = sum((budget.amount for budget in budgets), Decimal("0"))
            budget_amount_by_category = {budget.category_id: budget.amount for budget in budgets}
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
            spending_res = SpendingSummary(
                status="available",
                month_spent=month_spent,
                month_budget=month_budget,
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
# Budget Guardrails Workflow
# ---------------------------------------------------------------------------


def _snapshot_todo(todo: Todo) -> dict:
    return {
        "title": todo.title,
        "description": todo.description,
        "due_date": todo.due_date.isoformat() if todo.due_date else None,
        "priority": todo.priority,
        "completed": todo.completed,
    }


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
            SpendingBudget.month_start == month_start,
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
                next_due = _advance_due_date(
                    recurrence.next_due_date, recurrence.frequency, recurrence.interval
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
            recurrence.next_due_date = _advance_due_date(
                recurrence.next_due_date, recurrence.frequency, recurrence.interval
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
                next_due = _advance_due_date(rule.next_due_date, rule.frequency, rule.interval)
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
            rule.next_due_date = _advance_due_date(
                rule.next_due_date, rule.frequency, rule.interval
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
