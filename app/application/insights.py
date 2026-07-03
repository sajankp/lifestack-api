"""Dashboard insight detectors (spec-058).

Three independent, stateless detectors — spending anomaly vs a trailing
4-week average, budget pace forecast, and new recurring-charge detection —
each writing plain ``Notification`` rows (``category="insight"``). No new
tables: dedup is a targeted existence check against ``Notification``
(``entity_type`` + ``entity_public_id`` + a period marker embedded in
``body``) rather than a unique constraint, since ``Notification`` is a
shared table used by every other notification category too.

Called from ``app.application.jobs.dashboard_insights_job`` via
``run_workspace_job``; contains no locking/session-boundary concerns of its
own (those live in jobs.py per that module's boundary rules).
"""

import calendar
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.notifications.models import Notification
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService
from app.platform.models import Workspace, WorkspaceMembership
from app.spending.models import (
    RecurringTransaction,
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
    TransactionType,
)

logger = structlog.get_logger(__name__)

_ANOMALY_RATIO = Decimal("1.5")
_ANOMALY_FLOOR = Decimal("500")
_PACE_MIN_DAYS_ELAPSED = 5
_PACE_BUFFER_RATIO = Decimal("1.1")
_RECURRING_LOOKBACK_DAYS = 65
_RECURRING_TOLERANCE_RATIO = Decimal("0.05")


async def _resolve_primary_user_id(session: AsyncSession, workspace_id: int) -> int | None:
    result = await session.execute(
        select(WorkspaceMembership.user_id)
        .where(WorkspaceMembership.workspace_id == workspace_id)
        .order_by(
            (WorkspaceMembership.role == "owner").desc(), WorkspaceMembership.created_at.asc()
        )
        .limit(1)
    )
    return result.scalar()


async def _insight_already_exists(
    session: AsyncSession,
    workspace_id: int,
    user_id: int,
    entity_type: str,
    entity_public_id,
    since: datetime | None,
) -> bool:
    query = select(Notification.id).where(
        Notification.workspace_id == workspace_id,
        Notification.user_id == user_id,
        Notification.category == "insight",
        Notification.entity_type == entity_type,
        Notification.entity_public_id == entity_public_id,
    )
    if since is not None:
        query = query.where(Notification.created_at >= since)
    result = await session.execute(query.limit(1))
    return result.scalar() is not None


def _week_start(today: date) -> date:
    return today - timedelta(days=today.weekday())


async def _detect_spending_anomalies(
    session: AsyncSession,
    workspace: Workspace,
    user_id: int,
    notification_service: NotificationService,
    today: date,
) -> None:
    week_start = _week_start(today)
    current_start = datetime.combine(today - timedelta(days=7), datetime.min.time(), tzinfo=UTC)
    current_end = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
    baseline_start = datetime.combine(today - timedelta(days=35), datetime.min.time(), tzinfo=UTC)

    categories = (
        (
            await session.execute(
                select(SpendingCategory).where(SpendingCategory.workspace_id == workspace.id)
            )
        )
        .scalars()
        .all()
    )

    for category in categories:
        current_week_total = (
            await session.execute(
                select(SpendingTransaction.amount).where(
                    SpendingTransaction.workspace_id == workspace.id,
                    SpendingTransaction.category_id == category.id,
                    SpendingTransaction.type == TransactionType.expense,
                    SpendingTransaction.occurred_at >= current_start,
                    SpendingTransaction.occurred_at < current_end,
                )
            )
        ).scalars()
        current_total = sum(current_week_total, Decimal("0"))
        if current_total <= 0:
            continue

        baseline_amounts = (
            await session.execute(
                select(SpendingTransaction.amount).where(
                    SpendingTransaction.workspace_id == workspace.id,
                    SpendingTransaction.category_id == category.id,
                    SpendingTransaction.type == TransactionType.expense,
                    SpendingTransaction.occurred_at >= baseline_start,
                    SpendingTransaction.occurred_at < current_start,
                )
            )
        ).scalars()
        baseline_total = sum(baseline_amounts, Decimal("0"))
        baseline_avg = baseline_total / Decimal("4")

        if baseline_avg <= 0:
            continue
        if current_total < baseline_avg * _ANOMALY_RATIO:
            continue
        if current_total < baseline_avg + _ANOMALY_FLOOR:
            continue

        if await _insight_already_exists(
            session,
            workspace.id,
            user_id,
            "spending_category_anomaly",
            category.public_id,
            datetime.combine(week_start, datetime.min.time(), tzinfo=UTC),
        ):
            continue

        await notification_service.notify(
            workspace_id=workspace.id,
            user_id=user_id,
            category="insight",
            severity="warning",
            title=f"{category.name} spending is up this week",
            body=(
                f"{category.name} spending this week is {current_total:.2f}, "
                f"vs a trailing 4-week average of {baseline_avg:.2f}."
            ),
            module="spending",
            entity_type="spending_category_anomaly",
            entity_public_id=category.public_id,
        )


async def _detect_budget_pace(
    session: AsyncSession,
    workspace: Workspace,
    user_id: int,
    notification_service: NotificationService,
    today: date,
) -> None:
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_elapsed = today.day
    if days_elapsed < _PACE_MIN_DAYS_ELAPSED:
        return

    month_start = today.replace(day=1)
    month_start_dt = datetime.combine(month_start, datetime.min.time(), tzinfo=UTC)
    today_dt = datetime.combine(today, datetime.min.time(), tzinfo=UTC)

    budgets = (
        (
            await session.execute(
                select(SpendingBudget).where(
                    SpendingBudget.workspace_id == workspace.id,
                    SpendingBudget.month_start == month_start,
                )
            )
        )
        .scalars()
        .all()
    )
    if not budgets:
        return

    category_map = {
        c.id: c
        for c in (
            await session.execute(
                select(SpendingCategory).where(SpendingCategory.workspace_id == workspace.id)
            )
        )
        .scalars()
        .all()
    }

    for budget in budgets:
        category = category_map.get(budget.category_id)
        if category is None:
            continue

        spent_amounts = (
            await session.execute(
                select(SpendingTransaction.amount).where(
                    SpendingTransaction.workspace_id == workspace.id,
                    SpendingTransaction.category_id == budget.category_id,
                    SpendingTransaction.type == TransactionType.expense,
                    SpendingTransaction.occurred_at >= month_start_dt,
                    SpendingTransaction.occurred_at < today_dt,
                )
            )
        ).scalars()
        spent_so_far = sum(spent_amounts, Decimal("0"))
        if spent_so_far <= 0:
            continue

        projected = spent_so_far / Decimal(days_elapsed) * Decimal(days_in_month)
        if projected <= budget.amount * _PACE_BUFFER_RATIO:
            continue

        if await _insight_already_exists(
            session,
            workspace.id,
            user_id,
            "spending_budget_pace",
            budget.public_id,
            month_start_dt,
        ):
            continue

        await notification_service.notify(
            workspace_id=workspace.id,
            user_id=user_id,
            category="insight",
            severity="warning",
            title=f"On pace to exceed {category.name} budget",
            body=(
                f"{category.name}: {spent_so_far:.2f} spent so far, "
                f"projected {projected:.2f} vs a budget of {budget.amount:.2f}."
            ),
            module="spending",
            entity_type="spending_budget_pace",
            entity_public_id=budget.public_id,
        )


def _amounts_within_tolerance(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(a, b) * _RECURRING_TOLERANCE_RATIO


async def _detect_recurring_candidates(
    session: AsyncSession,
    workspace: Workspace,
    user_id: int,
    notification_service: NotificationService,
    today: date,
) -> None:
    lookback_start = datetime.combine(
        today - timedelta(days=_RECURRING_LOOKBACK_DAYS), datetime.min.time(), tzinfo=UTC
    )
    today_dt = datetime.combine(today, datetime.min.time(), tzinfo=UTC)

    transactions = (
        (
            await session.execute(
                select(SpendingTransaction).where(
                    SpendingTransaction.workspace_id == workspace.id,
                    SpendingTransaction.type == TransactionType.expense,
                    SpendingTransaction.occurred_at >= lookback_start,
                    SpendingTransaction.occurred_at < today_dt,
                )
            )
        )
        .scalars()
        .all()
    )
    if not transactions:
        return

    by_category: dict[int, list[SpendingTransaction]] = {}
    for txn in transactions:
        by_category.setdefault(txn.category_id, []).append(txn)

    active_rules = (
        (
            await session.execute(
                select(RecurringTransaction).where(
                    RecurringTransaction.workspace_id == workspace.id,
                    RecurringTransaction.is_active,
                )
            )
        )
        .scalars()
        .all()
    )
    rules_by_category: dict[int, list[RecurringTransaction]] = {}
    for rule in active_rules:
        rules_by_category.setdefault(rule.category_id, []).append(rule)

    category_map = {
        c.id: c
        for c in (
            await session.execute(
                select(SpendingCategory).where(SpendingCategory.workspace_id == workspace.id)
            )
        )
        .scalars()
        .all()
    }

    for category_id, txns in by_category.items():
        category = category_map.get(category_id)
        if category is None:
            continue

        # Bucket by mutual amount tolerance (greedy — each transaction joins
        # the first compatible bucket it matches).
        buckets: list[list[SpendingTransaction]] = []
        for txn in txns:
            for bucket in buckets:
                if _amounts_within_tolerance(txn.amount, bucket[0].amount):
                    bucket.append(txn)
                    break
            else:
                buckets.append([txn])

        for bucket in buckets:
            distinct_months = {(t.occurred_at.year, t.occurred_at.month) for t in bucket}
            if len(distinct_months) < 2:
                continue

            bucket_amount = bucket[0].amount
            already_tracked = any(
                _amounts_within_tolerance(bucket_amount, rule.amount)
                for rule in rules_by_category.get(category_id, [])
            )
            if already_tracked:
                continue

            if await _insight_already_exists(
                session,
                workspace.id,
                user_id,
                "spending_category_recurring",
                category.public_id,
                None,
            ):
                continue

            await notification_service.notify(
                workspace_id=workspace.id,
                user_id=user_id,
                category="insight",
                severity="info",
                title=f"Recurring charge detected: {category.name}",
                body=(
                    f"{category.name} has a repeating charge of about {bucket_amount:.2f} "
                    f"across {len(distinct_months)} months. Consider adding a recurring rule."
                ),
                module="spending",
                entity_type="spending_category_recurring",
                entity_public_id=category.public_id,
            )
            # One candidate notification per category per run is enough —
            # move on rather than flagging every bucket in the same category.
            break


async def generate_workspace_insights(
    session: AsyncSession, workspace: Workspace, *, today: date | None = None
) -> None:
    """Run all three detectors for a single workspace, in its own session/transaction
    (caller — ``dashboard_insights_job`` via ``run_workspace_job`` — owns that boundary).

    ``today`` defaults to the real current date; tests pass an explicit value
    so the budget-pace detector's "at least N days into the month" guard
    doesn't make tests flaky depending on which day they happen to run on.
    """
    if workspace.id is None:
        return

    user_id = await _resolve_primary_user_id(session, workspace.id)
    if user_id is None:
        logger.warning("dashboard_insights_no_members", workspace_id=workspace.id)
        return

    notification_repo = NotificationRepository(session)
    notification_service = NotificationService(notification_repo)
    if today is None:
        today = datetime.now(UTC).date()

    await _detect_spending_anomalies(session, workspace, user_id, notification_service, today)
    await _detect_budget_pace(session, workspace, user_id, notification_service, today)
    await _detect_recurring_candidates(session, workspace, user_id, notification_service, today)
