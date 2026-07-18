from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import date_in_any_window, find_import_revert_windows
from app.core.exceptions import NotFoundError
from app.finance.models import Account, NetWorthSnapshot
from app.finance.repository import (
    AccountRepository,
    CurrencyRepository,
    FinanceSettingRepository,
    FxRateRepository,
    NetWorthSnapshotRepository,
)
from app.finance.service import FxRateService
from app.health.repository import (
    MedicationEventRepository,
    MedicationRepository,
    WeightEntryRepository,
)
from app.health.schedule import get_dose_slots_for_date
from app.investing.models import Dividend, PortfolioSnapshot
from app.investing.repository import (
    DividendRepository,
    HoldingPriceRepository,
    HoldingRepository,
    InvestingOrderRepository,
)
from app.investing.return_metrics_service import ReturnMetricsService
from app.notifications.service import NotificationService
from app.spending.models import (
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
    TransactionType,
)
from app.summaries.models import WeeklySummary, WorkspaceSummarySetting
from app.summaries.repository import WeeklySummaryRepository, WorkspaceSummarySettingRepository
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

    async def has_reverted_import_overlap(self, item: WeeklySummary) -> bool:
        """spec-086 Layers 2-3: whether this summary's net-worth/investing
        boundary snapshot dates overlap a since-reverted import's live
        window -- i.e. the existing "complete" figure may reflect data that
        was later reverted. Distinct from is_stale (a FRESHER snapshot
        exists): here the snapshot itself can never be corrected (see
        spec-086 "Why restatement is not viable" -- historical quantities/
        prices aren't reconstructable), so annotation is the only honest
        signal. Sourced from the append-only import_rolled_back audit
        trail, not from the (already-deleted) ImportBatch row."""
        dates: list[date] = []
        if item.net_worth_summary and item.net_worth_summary.get("status") == "complete":
            start_nw = item.net_worth_summary.get("start_snapshot_date")
            end_nw = item.net_worth_summary.get("end_snapshot_date")
            if start_nw:
                dates.append(date.fromisoformat(start_nw))
            if end_nw:
                dates.append(date.fromisoformat(end_nw))
        if item.investing_summary and item.investing_summary.get("status") == "complete":
            start_inv = item.investing_summary.get("start_snapshot_date")
            end_inv = item.investing_summary.get("end_snapshot_date")
            if start_inv:
                dates.append(date.fromisoformat(start_inv))
            if end_inv:
                dates.append(date.fromisoformat(end_inv))
        if not dates:
            return False

        windows = await find_import_revert_windows(
            self.session, item.workspace_id, min(dates), max(dates)
        )
        if not windows:
            return False
        return any(date_in_any_window(d, windows) for d in dates)

    async def is_stale(self, item: WeeklySummary) -> bool:
        """spec-085: whether fresher net-worth/investing boundary data now
        exists than what this stored summary was generated from -- a cheap,
        read-time-only check (two indexed single-row queries), not a stored
        column. Only checked for sections that reported "complete" at
        generation time; an "unavailable" section has no boundary date to
        compare against and a newly-available snapshot there is a status
        change, not staleness of an existing figure."""
        if item.net_worth_summary and item.net_worth_summary.get("status") == "complete":
            end_snapshot = (
                await self.session.execute(
                    select(NetWorthSnapshot)
                    .where(
                        NetWorthSnapshot.workspace_id == item.workspace_id,
                        NetWorthSnapshot.snapshot_date <= item.week_end,
                    )
                    .order_by(NetWorthSnapshot.snapshot_date.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            stored_date = item.net_worth_summary.get("end_snapshot_date")
            if end_snapshot is not None and end_snapshot.snapshot_date.isoformat() != stored_date:
                return True

        if item.investing_summary and item.investing_summary.get("status") == "complete":
            end_snapshot = (
                await self.session.execute(
                    select(PortfolioSnapshot)
                    .where(
                        PortfolioSnapshot.workspace_id == item.workspace_id,
                        PortfolioSnapshot.snapshot_date <= item.week_end,
                    )
                    .order_by(PortfolioSnapshot.snapshot_date.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            stored_date = item.investing_summary.get("end_snapshot_date")
            if end_snapshot is not None and end_snapshot.snapshot_date.isoformat() != stored_date:
                return True

        return False

    async def mark_read(self, workspace_id: int, public_id: uuid.UUID):
        """Record that the user has opened this summary (spec-080). Workspace-scoped
        404 for unknown/other-workspace ids; idempotent on repeat reads."""
        item = await self.repository.by_public_id(workspace_id, public_id)
        if not item:
            raise NotFoundError(detail=f"Weekly summary with id {public_id} not found")
        return await self.repository.mark_read(item)

    async def generate_for_workspace_week(
        self, workspace_id: int, user_id: int, week_start: date
    ) -> WeeklySummary:
        week_end, sections = await self._compose(workspace_id, week_start)

        existing = (
            await self.session.execute(
                select(WeeklySummary).where(
                    WeeklySummary.workspace_id == workspace_id,
                    WeeklySummary.week_start == week_start,
                    # A superseded row keeps its week_start (retained, not
                    # deleted) -- only the current version should be matched
                    # for the upsert-by-week path; the partial unique index
                    # (uq_weekly_summary_workspace_week_current) guarantees
                    # at most one such row exists.
                    WeeklySummary.superseded_by_id.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.week_end = week_end
            for field, value in sections.items():
                setattr(existing, field, value)
            existing.generated_at = datetime.now(UTC)
            summary = existing
        else:
            summary = WeeklySummary(
                workspace_id=workspace_id,
                week_start=week_start,
                week_end=week_end,
                **sections,
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

    async def regenerate(
        self, workspace_id: int, public_id: uuid.UUID, reason: str | None
    ) -> WeeklySummary:
        """spec-076 manual regeneration: recomputes the same period from
        current data. Versioned, not destructive — the old row is retained
        (never deleted, no cap) and marked superseded; the new row carries
        the regeneration trail. Deliberately does NOT call
        notification_service (a regenerate is a bookkeeping correction, not
        a new event the user needs to be notified about)."""
        old = await self.repository.by_public_id(workspace_id, public_id)
        if not old:
            raise NotFoundError(detail=f"Weekly summary with id {public_id} not found")
        if old.superseded_by_id is not None:
            raise NotFoundError(
                detail=f"Weekly summary with id {public_id} has already been superseded"
            )

        week_end, sections = await self._compose(workspace_id, old.week_start)
        new = WeeklySummary(
            workspace_id=workspace_id,
            week_start=old.week_start,
            week_end=week_end,
            **sections,
        )
        return await self.repository.supersede(old, new, reason)

    async def _compose(self, workspace_id: int, week_start: date) -> tuple[date, dict]:
        """Computes every section + highlights for one workspace/week from
        current data. Shared by generate_for_workspace_week (upsert path) and
        regenerate (supersede path) so both recompute the same fields."""
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
        dividend_summary = await self._dividend_summary(workspace_id, week_start, week_end)
        net_worth_summary = await self._net_worth_summary(workspace_id, week_start, week_end)
        return_metrics_summary = await self._return_metrics_summary(workspace_id)
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
        if dividend_summary["status"] == "complete" and dividend_summary["count"] > 0:
            flags.append({
                "type": "dividend_income",
                "message": f"Received {dividend_summary['count']} dividend/income payment(s) this week.",
            })
        if return_metrics_summary["status"] == "complete" and return_metrics_summary["notable"]:
            flags.append({
                "type": "drawdown_notable",
                "message": "Portfolio drawdown from peak is notable — check the return metrics section.",
            })

        return week_end, {
            "todo_summary": todo_summary,
            "spending_summary": spending_summary,
            "investing_summary": investing_summary,
            "health_summary": health_summary,
            "dividend_summary": dividend_summary,
            "net_worth_summary": net_worth_summary,
            "return_metrics_summary": return_metrics_summary,
            "highlights": {"flags": flags},
        }

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

        if scheduled_count == 0 and taken_count == 0 and skipped_count == 0 and not weight_entries:
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

    async def _dividend_summary(self, workspace_id: int, week_start: date, week_end: date) -> dict:
        """Dividend/interest income received in the period (spec-073 events).
        Zero-activity weeks are "complete" with zero totals, matching the
        spending/investing convention — "unavailable" is reserved for a
        genuine data-quality problem (here: mixed currencies)."""
        rows = list(
            (
                await self.session.execute(
                    select(Dividend).where(
                        Dividend.workspace_id == workspace_id,
                        Dividend.pay_date >= week_start,
                        Dividend.pay_date <= week_end,
                    )
                )
            )
            .scalars()
            .all()
        )
        currencies = sorted({row.currency for row in rows})
        if len(currencies) > 1:
            return {
                "status": "unavailable",
                "total_net": None,
                "currency": None,
                "count": len(rows),
                "by_symbol": [],
                "has_multiple_currencies": True,
            }

        money = Decimal("0.01")
        currency = currencies[0] if currencies else None
        total_net = sum((row.net_amount for row in rows), Decimal("0"))
        by_symbol: dict[str, Decimal] = {}
        for row in rows:
            key = row.symbol or "Interest"
            by_symbol[key] = by_symbol.get(key, Decimal("0")) + row.net_amount
        return {
            "status": "complete",
            "total_net": str(total_net.quantize(money)),
            "currency": currency,
            "count": len(rows),
            "by_symbol": [
                {"symbol": symbol, "net_amount": str(amount.quantize(money))}
                for symbol, amount in sorted(
                    by_symbol.items(), key=lambda item: item[1], reverse=True
                )[:5]
            ],
            "has_multiple_currencies": False,
        }

    async def _net_worth_summary(self, workspace_id: int, week_start: date, week_end: date) -> dict:
        """Net-worth change with as-of provenance (spec-065 snapshots) —
        mirrors _investing_summary's start/end-snapshot-diff shape exactly,
        including the same "unavailable" fallback when a baseline is
        missing or the reporting currency changed mid-comparison."""
        start_snapshot = (
            await self.session.execute(
                select(NetWorthSnapshot)
                .where(
                    NetWorthSnapshot.workspace_id == workspace_id,
                    NetWorthSnapshot.snapshot_date < week_start,
                )
                .order_by(NetWorthSnapshot.snapshot_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        end_snapshot = (
            await self.session.execute(
                select(NetWorthSnapshot)
                .where(
                    NetWorthSnapshot.workspace_id == workspace_id,
                    NetWorthSnapshot.snapshot_date <= week_end,
                )
                .order_by(NetWorthSnapshot.snapshot_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if (
            start_snapshot is None
            or end_snapshot is None
            or end_snapshot.snapshot_date < week_start
            or start_snapshot.reporting_currency != end_snapshot.reporting_currency
        ):
            return {
                "status": "unavailable",
                "net_worth_start": None,
                "net_worth_end": None,
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

        week_change = end_snapshot.total_net_worth - start_snapshot.total_net_worth
        week_change_pct = (
            (week_change / start_snapshot.total_net_worth) * Decimal("100")
            if start_snapshot.total_net_worth != 0
            else None
        )
        money = Decimal("0.01")
        return {
            "status": "complete",
            "net_worth_start": str(start_snapshot.total_net_worth.quantize(money)),
            "net_worth_end": str(end_snapshot.total_net_worth.quantize(money)),
            "week_change": str(week_change.quantize(money)),
            "week_change_pct": (
                str(week_change_pct.quantize(money)) if week_change_pct is not None else None
            ),
            "currency": end_snapshot.reporting_currency,
            "start_snapshot_date": start_snapshot.snapshot_date.isoformat(),
            "end_snapshot_date": end_snapshot.snapshot_date.isoformat(),
        }

    async def _return_metrics_summary(self, workspace_id: int) -> dict:
        """Notable return-metric moves (spec-071). There is no historical
        return-metrics snapshot store, so unlike the sections above this is
        NOT a week-over-week delta — it is the current XIRR/annualized-return/
        max-drawdown state as of generation time, with "notable" flagging a
        drawdown-from-peak past a fixed threshold."""
        return_metrics_service = ReturnMetricsService(
            InvestingOrderRepository(self.session),
            HoldingRepository(self.session),
            HoldingPriceRepository(self.session),
            DividendRepository(self.session),
            AccountRepository(self.session),
            NetWorthSnapshotRepository(self.session),
            FxRateService(FxRateRepository(self.session), CurrencyRepository(self.session)),
            FinanceSettingRepository(self.session),
        )
        metrics = await return_metrics_service.get_return_metrics(workspace_id)
        overall = metrics.overall
        if metrics.valuation_status != "current" or overall.xirr is None:
            return {
                "status": "unavailable",
                "xirr": None,
                "annualized_return_pct": None,
                "max_drawdown_pct": None,
                "notable": False,
            }

        money = Decimal("0.01")
        drawdown_pct = overall.max_drawdown.pct if overall.max_drawdown else None
        notable = drawdown_pct is not None and drawdown_pct >= Decimal("10")
        return {
            "status": "complete",
            "xirr": str(overall.xirr.quantize(money)),
            "annualized_return_pct": (
                str(overall.annualized_return_pct.quantize(money))
                if overall.annualized_return_pct is not None
                else None
            ),
            "max_drawdown_pct": (
                str(drawdown_pct.quantize(money)) if drawdown_pct is not None else None
            ),
            "notable": notable,
        }


class SummarySettingsService:
    """Per-workspace weekly-summary cadence (spec-076). Deliberately separate
    from WeeklySummaryService — settings CRUD has nothing to do with summary
    composition, and keeping it apart avoids growing that constructor."""

    def __init__(self, repository: WorkspaceSummarySettingRepository):
        self.repository = repository

    async def get(self, workspace_id: int) -> WorkspaceSummarySetting | None:
        return await self.repository.get_by_workspace(workspace_id)

    async def update(
        self, workspace_id: int, cadence_day_of_week: int, cadence_hour_utc: int
    ) -> WorkspaceSummarySetting:
        return await self.repository.upsert(
            workspace_id,
            cadence_day_of_week=cadence_day_of_week,
            cadence_hour_utc=cadence_hour_utc,
        )
