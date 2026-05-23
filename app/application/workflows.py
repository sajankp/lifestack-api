from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import UserCreate
from app.auth.service import AuthService
from app.config import settings
from app.core.audit import AuditLogger
from app.platform.models import Workspace, WorkspaceMembership
from app.platform.service import WorkspaceService
from app.spending.models import SpendingBudget, SpendingCategory, SpendingTransaction
from app.spending.service import CategoryService
from app.todo.models import PriorityEnum, Todo

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

            if todo:
                # Update existing todo only when something actually changed
                before_snap = _snapshot_todo(todo)
                updated = False

                if todo.completed:
                    todo.completed = False
                    updated = True
                if todo.title != title:
                    todo.title = title
                    updated = True
                if todo.description != desc:
                    todo.description = desc
                    updated = True
                if todo.priority != priority:
                    todo.priority = priority
                    updated = True

                if updated:
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
                        "budget_guardrail_todo_updated",
                        workspace_id=workspace.id,
                        category=category.name,
                        severity=severity,
                        ratio=f"{ratio:.1%}",
                    )
            else:
                # Create a new system todo for this category breach
                todo = Todo(
                    workspace_id=workspace.id,
                    user_id=user_id,
                    title=title,
                    description=desc,
                    priority=priority,
                    system_key=system_key,
                )
                session.add(todo)
                await session.flush()
                after_snap = _snapshot_todo(todo)
                await audit_logger.log(
                    workspace_id=workspace.id,
                    actor_id=user_id,
                    action="budget_guardrail_triggered",
                    module="application",
                    entity_type="todo",
                    entity_id=todo.id,  # type: ignore[arg-type]
                    details={
                        "entity_public_id": str(todo.public_id),
                        "before": None,
                        "after": after_snap,
                        "changed_fields": list(after_snap.keys()),
                    },
                )
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
