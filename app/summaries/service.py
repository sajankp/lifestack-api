import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.investing.models import CashBalance, Holding
from app.notifications.service import NotificationService
from app.spending.models import SpendingTransaction, TransactionType
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
        spending_totals = (
            await self.session.execute(
                select(
                    SpendingTransaction.type,
                    func.coalesce(func.sum(SpendingTransaction.amount), 0),
                )
                .where(
                    SpendingTransaction.workspace_id == workspace_id,
                    SpendingTransaction.occurred_at >= start_dt,
                    SpendingTransaction.occurred_at < end_dt,
                )
                .group_by(SpendingTransaction.type)
            )
        ).all()
        spending_by_type = dict(spending_totals)
        income = spending_by_type.get(TransactionType.income, 0)
        expense = spending_by_type.get(TransactionType.expense, 0)

        holdings_value = (
            await self.session.execute(
                select(func.coalesce(func.sum(Holding.quantity * Holding.avg_cost), 0)).where(
                    Holding.workspace_id == workspace_id
                )
            )
        ).scalar_one()
        cash_value = (
            await self.session.execute(
                select(func.coalesce(func.sum(CashBalance.balance), 0)).where(
                    CashBalance.workspace_id == workspace_id
                )
            )
        ).scalar_one()

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
            existing.todo_summary = {
                "tasks_created": todo_created,
                "tasks_completed": todo_completed,
            }
            existing.spending_summary = {
                "total_income": str(income),
                "total_expense": str(expense),
                "net": str(income - expense),
            }
            existing.investing_summary = {
                "portfolio_value_end": str(holdings_value + cash_value),
                "currency": "USD",
            }
            existing.generated_at = datetime.now(UTC)
            summary = existing
        else:
            summary = WeeklySummary(
                workspace_id=workspace_id,
                week_start=week_start,
                week_end=week_end,
                todo_summary={"tasks_created": todo_created, "tasks_completed": todo_completed},
                spending_summary={
                    "total_income": str(income),
                    "total_expense": str(expense),
                    "net": str(income - expense),
                },
                investing_summary={
                    "portfolio_value_end": str(holdings_value + cash_value),
                    "currency": "USD",
                },
                highlights={"flags": []},
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
