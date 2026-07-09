from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.finance.models import Account
from app.health.repository import (
    MedicationEventRepository,
    MedicationRepository,
    WeightEntryRepository,
)
from app.health.schedule import get_dose_slots_for_date
from app.investing.models import PortfolioSnapshot
from app.notifications.service import NotificationService
from app.spending.models import (
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
    TransactionType,
)
from app.summaries.models import WeeklySummary
from app.summaries.repository import WeeklySummaryRepository
from app.todo.models import Todo


class WeeklySummaryService:
    def __init__(
        self,
        repository: WeeklySummaryRepository,
        session: AsyncSession,
        notification_service: NotificationService,
    ):
        self.repository = repository
        self.session = session
        self.notification_service = notification_service

    async def list(
        self,
        workspace_id: int,
        from_date: date | None,
        to_date: date | None,
        limit: int,
        offset: int,
    ):
        return await self.repository.list(workspace_id, from_date, to_date, limit, offset)

    async def latest(self, workspace_id: int):
        item = await self.repository.latest(workspace_id)
        if not item:
            raise NotFoundError(detail="No weekly summaries found")
        return item

    async def get(self, workspace_id: int, public_id: uuid.UUID):
        item = await self.repository.by_public_id(workspace_id, public_id)
        if not item:
            raise NotFoundError(detail=f"Weekly summary with id {public_id} not found")
        return item

    async def generate_for_workspace_week(
        self, workspace_id: int, user_id: int, week_start: date
    ) -> WeeklySummary:
        week_end = week_start + timedelta(days=6)
        start_dt = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)
        end_dt = datetime.combine(week_end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)

        todo_created = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Todo)
                    .where(
                        Todo.workspace_id == workspace_id,
                        Todo.created_at >= start_dt,
                        Todo.created_at < end_dt,
                    )
                )
            ).scalar_one()
        )
        todo_completed = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Todo)
                    .where(
                        Todo.workspace_id == workspace_id,
                        Todo.completed.is_(True),
                        Todo.updated_at >= start_dt,
                        Todo.updated_at < end_dt,
                    )
                )
            ).scalar_one()
        )
        spending_rows = (
            await self.session.execute(
                select(
                    SpendingTransaction.type,
                    SpendingTransaction.amount,
                    SpendingTransaction.category_id,
                    SpendingTransaction.recurring_transaction_id,
                    Account.default_currency_code,
                )
                .outerjoin(Account, Account.id == SpendingTransaction.account_id)
                .where(
                    SpendingTransaction.workspace_id == workspace_id,
                    SpendingTransaction.occurred_at >= start_dt,
                    SpendingTransaction.occurred_at < end_dt,
                )
            )
        ).all()
        spending_summary, category_expenses = await self._spending_summary(
            workspace_id, week_start, spending_rows
        )
        todo_overdue = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Todo)
                    .where(
                        Todo.workspace_id == workspace_id,
                        Todo.due_date.is_not(None),
                        Todo.due_date < end_dt,
                        or_(
                            Todo.completed.is_(False),
                            Todo.updated_at >= end_dt,
                        ),
                    )
                )
            ).scalar_one()
        )
        open_count_start = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Todo)
                    .where(
                        Todo.workspace_id == workspace_id,
                        Todo.created_at < start_dt,
                        or_(
                            Todo.completed.is_(False),
                            Todo.updated_at >= start_dt,
                        ),
                    )
                )
            ).scalar_one()
        )
        open_count_end = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Todo)
                    .where(
                        Todo.workspace_id == workspace_id,
                        Todo.created_at < end_dt,
                        or_(
                            Todo.completed.is_(False),
                            Todo.updated_at >= end_dt,
                        ),
                    )
                )
            ).scalar_one()
        )
        completion_rate = (
            (Decimal(todo_completed) / Decimal(todo_created) * Decimal("100"))
            if todo_created > 0
            else None
        )
        todo_summary = {
            "tasks_created": todo_created,
            "tasks_completed": todo_completed,
            "tasks_overdue": todo_overdue,
            "completion_rate_pct": (
                str(completion_rate.quantize(Decimal("0.1")))
                if completion_rate is not None
                else None
            ),
            "open_count_start": open_count_start,
            "open_count_end": open_count_end,
        }

        investing_summary = await self._investing_summary(workspace_id, week_start, week_end)
        health_summary = await self._health_summary(
            workspace_id, week_start, week_end, start_dt, end_dt
        )
        flags: list[dict[str, str]] = []
        if completion_rate is not None and completion_rate >= Decimal("90"):
            flags.append({
                "type": "high_completion",
                "message": f"Completed {todo_completed} tasks this week.",
            })
        elif todo_created >= 5 and completion_rate is not None and completion_rate < Decimal("50"):
            flags.append({
                "type": "low_completion",
                "message": "Less than half of this week's created tasks were completed.",
            })
        for category in category_expenses:
            if category["budget_breached"]:
                flags.append({
                    "type": "budget_breach",
                    "message": f"{category['name']} exceeded its configured budget.",
                })

        existing = (
            await self.session.execute(
                select(WeeklySummary).where(
                    WeeklySummary.workspace_id == workspace_id,
                    WeeklySummary.week_start == week_start,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.week_end = week_end
            existing.todo_summary = todo_summary
            existing.spending_summary = spending_summary
            existing.investing_summary = investing_summary
            existing.health_summary = health_summary
            existing.highlights = {"flags": flags}
            existing.generated_at = datetime.now(UTC)
            summary = existing
        else:
            summary = WeeklySummary(
                workspace_id=workspace_id,
                week_start=week_start,
                week_end=week_end,
                todo_summary=todo_summary,
                spending_summary=spending_summary,
                investing_summary=investing_summary,
                health_summary=health_summary,
                highlights={"flags": flags},
            )
            self.session.add(summary)
        await self.session.flush()
        await self.notification_service.notify(
            workspace_id=workspace_id,
            user_id=user_id,
            category="system",
            severity="info",
            title=f"Weekly summary ready: {week_start.isoformat()}",
            module="application",
        )
        return summary

    async def _spending_summary(
        self,
        workspace_id: int,
        week_start: date,
        rows: list,
    ) -> tuple[dict, list[dict]]:
        currencies = sorted({
            row.default_currency_code for row in rows if row.default_currency_code
        })
        has_unknown_currency = any(row.default_currency_code is None for row in rows)
        breakdown: dict[str, dict[str, Decimal]] = {}
        category_totals: dict[int, Decimal] = {}
        for row in rows:
            currency = row.default_currency_code or "UNKNOWN"
            bucket = breakdown.setdefault(
                currency, {"income": Decimal("0"), "expense": Decimal("0")}
            )
            bucket[row.type] += row.amount
            if row.type == TransactionType.expense.value:
                category_totals[row.category_id] = (
                    category_totals.get(row.category_id, Decimal("0")) + row.amount
                )

        unavailable = len(currencies) > 1 or has_unknown_currency
        if unavailable:
            return (
                {
                    "status": "unavailable",
                    "total_income": None,
                    "total_expense": None,
                    "net": None,
                    "currency": None,
                    "has_multiple_currencies": len(currencies) > 1,
                    "currency_breakdown": {
                        code: {key: str(value) for key, value in totals.items()}
                        for code, totals in breakdown.items()
                    },
                    "top_categories": [],
                    "budget_utilization_pct": None,
                    "budgets_breached": 0,
                    "recurring_generated_count": 0,
                },
                [],
            )

        currency = currencies[0] if currencies else None
        totals = breakdown.get(currency or "UNKNOWN", {})
        income = totals.get("income", Decimal("0"))
        expense = totals.get("expense", Decimal("0"))
        month_start = week_start.replace(day=1)
        category_ids = list(category_totals)
        categories = {}
        budgets = {}
        if category_ids:
            categories = {
                row.id: row
                for row in (
                    await self.session.execute(
                        select(SpendingCategory).where(
                            SpendingCategory.workspace_id == workspace_id,
                            SpendingCategory.id.in_(category_ids),
                        )
                    )
                ).scalars()
            }
            budgets = {
                row.category_id: row.amount
                for row in (
                    await self.session.execute(
                        select(SpendingBudget).where(
                            SpendingBudget.workspace_id == workspace_id,
                            and_(
                                SpendingBudget.start_month <= month_start,
                                or_(
                                    SpendingBudget.end_month.is_(None),
                                    SpendingBudget.end_month >= month_start,
                                ),
                            ),
                            SpendingBudget.category_id.in_(category_ids),
                        )
                    )
                ).scalars()
            }
        category_details = []
        for category_id, amount in sorted(
            category_totals.items(), key=lambda item: item[1], reverse=True
        ):
            budget = budgets.get(category_id)
            category_details.append({
                "name": categories[category_id].name
                if category_id in categories
                else "Uncategorized",
                "amount": str(amount),
                "pct_of_total": str(((amount / expense) * Decimal("100")).quantize(Decimal("0.1")))
                if expense > 0
                else "0.0",
                "budget_breached": budget is not None and amount > budget,
            })
        total_budget = sum(budgets.values(), Decimal("0"))
        recurring_generated_count = sum(
            1 for row in rows if getattr(row, "recurring_transaction_id", None) is not None
        )
        return (
            {
                "status": "complete",
                "total_income": str(income),
                "total_expense": str(expense),
                "net": str(income - expense),
                "currency": currency,
                "has_multiple_currencies": False,
                "currency_breakdown": {},
                "top_categories": [
                    {key: value for key, value in item.items() if key != "budget_breached"}
                    for item in category_details[:5]
                ],
                "budget_utilization_pct": str(
                    ((expense / total_budget) * Decimal("100")).quantize(Decimal("0.1"))
                )
                if total_budget > 0
                else None,
                "budgets_breached": sum(1 for item in category_details if item["budget_breached"]),
                "recurring_generated_count": recurring_generated_count,
            },
            category_details,
        )

    async def _investing_summary(
        self, workspace_id: int, week_start: date, week_end: date
    ) -> dict[str, str | None]:
        start_snapshot = (
            await self.session.execute(
                select(PortfolioSnapshot)
                .where(
                    PortfolioSnapshot.workspace_id == workspace_id,
                    PortfolioSnapshot.snapshot_date < week_start,
                )
                .order_by(PortfolioSnapshot.snapshot_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        end_snapshot = (
            await self.session.execute(
                select(PortfolioSnapshot)
                .where(
                    PortfolioSnapshot.workspace_id == workspace_id,
                    PortfolioSnapshot.snapshot_date <= week_end,
                )
                .order_by(PortfolioSnapshot.snapshot_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if (
            start_snapshot is None
            or end_snapshot is None
            or end_snapshot.snapshot_date < week_start
            or start_snapshot.currency_code != end_snapshot.currency_code
        ):
            return {
                "status": "unavailable",
                "portfolio_value_start": None,
                "portfolio_value_end": None,
                "cash_start": None,
                "cash_end": None,
                "week_change": None,
                "week_change_pct": None,
                "currency": None,
                "start_snapshot_date": (
                    start_snapshot.snapshot_date.isoformat() if start_snapshot else None
                ),
                "end_snapshot_date": (
                    end_snapshot.snapshot_date.isoformat() if end_snapshot else None
                ),
            }

        week_change = end_snapshot.holdings_value - start_snapshot.holdings_value
        week_change_pct = (
            (week_change / start_snapshot.holdings_value) * Decimal("100")
            if start_snapshot.holdings_value != 0
            else None
        )
        money = Decimal("0.01")
        return {
            "status": "complete",
            "portfolio_value_start": str(start_snapshot.holdings_value.quantize(money)),
            "portfolio_value_end": str(end_snapshot.holdings_value.quantize(money)),
            "cash_start": str(start_snapshot.cash_value.quantize(money)),
            "cash_end": str(end_snapshot.cash_value.quantize(money)),
            "week_change": str(week_change.quantize(money)),
            "week_change_pct": (
                str(week_change_pct.quantize(money)) if week_change_pct is not None else None
            ),
            "currency": end_snapshot.currency_code,
            "start_snapshot_date": start_snapshot.snapshot_date.isoformat(),
            "end_snapshot_date": end_snapshot.snapshot_date.isoformat(),
        }

    async def _health_summary(
        self,
        workspace_id: int,
        week_start: date,
        week_end: date,
        start_dt: datetime,
        end_dt: datetime,
    ) -> dict | None:
        """Doses scheduled/taken/missed + adherence %, weight delta
        (spec-069 §C) — additive, omitted (None) when the workspace has no
        health data at all that week."""
        medication_repo = MedicationRepository(self.session)
        event_repo = MedicationEventRepository(self.session)
        weight_repo = WeightEntryRepository(self.session)

        medications, _total = await medication_repo.get_all(
            workspace_id, is_active=None, limit=1000
        )
        scheduled_count = 0
        day = week_start
        while day <= week_end:
            for med in medications:
                scheduled_count += len(get_dose_slots_for_date(med, day))
            day += timedelta(days=1)

        status_counts = await event_repo.get_status_counts_for_workspace(
            workspace_id, start_dt, end_dt
        )
        taken_count = status_counts.get("taken", 0)
        skipped_count = status_counts.get("skipped", 0)
        missed_count = max(0, scheduled_count - taken_count - skipped_count)
        adherence_pct = (
            str(
                (Decimal(taken_count) / Decimal(scheduled_count) * Decimal("100")).quantize(
                    Decimal("0.1")
                )
            )
            if scheduled_count > 0
            else None
        )

        weight_entries, _wtotal = await weight_repo.get_range(
            workspace_id, start_dt, end_dt, limit=1000, offset=0
        )
        weight_delta_kg = None
        if len(weight_entries) >= 2:
            ordered = sorted(weight_entries, key=lambda e: e.measured_at)
            weight_delta_kg = str(ordered[-1].weight_kg - ordered[0].weight_kg)

        if scheduled_count == 0 and not weight_entries:
            return None

        return {
            "doses_scheduled": scheduled_count,
            "doses_taken": taken_count,
            "doses_skipped": skipped_count,
            "doses_missed": missed_count,
            "adherence_pct": adherence_pct,
            "weight_entries_logged": len(weight_entries),
            "weight_delta_kg": weight_delta_kg,
        }
