from datetime import UTC, datetime
from decimal import Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.investing.service import InvestingSummaryService
from app.platform.models import Workspace, WorkspaceMembership
from app.platform.service import WorkspaceService
from app.spending.models import (
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
    TransactionType,
)
from app.spending.service import BudgetService, CategoryService, TransactionService
from app.todo.models import PriorityEnum, Todo
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
        investing_summary_service: InvestingSummaryService,
    ):
        self.todo_service = todo_service
        self.transaction_service = transaction_service
        self.budget_service = budget_service
        self.investing_summary_service = investing_summary_service

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
            investing_summary = await self.investing_summary_service.get_summary(workspace_id)
            investing_res = InvestingSummary(
                status="available",
                portfolio_value=investing_summary.portfolio_value,
                daily_change=investing_summary.daily_change,
                holdings_count=investing_summary.holdings_count,
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
