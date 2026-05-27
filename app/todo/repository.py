from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import DEFAULT_LIMIT
from app.todo.models import RecurringTodoRule, Todo


class TodoRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_all(
        self,
        workspace_id: int,
        completed: bool | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[Sequence[Todo], int]:
        base = select(Todo).where(Todo.workspace_id == workspace_id)
        if completed is not None:
            base = base.where(Todo.completed == completed)

        total_q = select(func.count()).select_from(base.subquery())
        total = (await self.session.execute(total_q)).scalar_one()

        items_q = base.order_by(Todo.created_at.desc()).limit(limit).offset(offset)
        result = await self.session.execute(items_q)
        return result.scalars().all(), total

    async def get_summary_counts(self, workspace_id: int, now: datetime) -> tuple[int, int]:
        """Return (open_count, overdue_count) using efficient SQL aggregation."""
        query = select(
            func.count().label("open_count"),
            func.sum(case((Todo.due_date < now, 1), else_=0)).label("overdue_count"),
        ).where(Todo.workspace_id == workspace_id, Todo.completed.is_(False))
        result = await self.session.execute(query)
        row = result.mappings().first()
        if not row:
            return 0, 0
        return row.get("open_count") or 0, row.get("overdue_count") or 0

    async def get_next_due_items(
        self, workspace_id: int, now: datetime, limit: int = 5
    ) -> Sequence[Todo]:
        result = await self.session.execute(
            select(Todo)
            .where(
                Todo.workspace_id == workspace_id,
                Todo.completed.is_(False),
                Todo.due_date.is_not(None),
                Todo.due_date >= now,
            )
            .order_by(Todo.due_date.asc())
            .limit(limit)
        )
        return result.scalars().all()

    async def get_active_guardrail_todo_count(self, workspace_id: int) -> int:
        result = await self.session.execute(
            select(func.count(Todo.id)).where(
                Todo.workspace_id == workspace_id,
                Todo.completed.is_(False),
                Todo.system_key.like("budget:guardrail:%"),
            )
        )
        return int(result.scalar() or 0)

    async def get_by_public_id(self, workspace_id: int, public_id: UUID) -> Todo | None:
        query = select(Todo).where(Todo.workspace_id == workspace_id, Todo.public_id == public_id)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def create(self, todo: Todo) -> Todo:
        self.session.add(todo)
        await self.session.flush()
        await self.session.refresh(todo)
        return todo

    async def delete(self, todo: Todo) -> None:
        await self.session.delete(todo)
        await self.session.flush()

    async def save(self, todo: Todo) -> Todo:
        self.session.add(todo)
        await self.session.flush()
        await self.session.refresh(todo)
        return todo

    async def get_recurring_rules(
        self,
        workspace_id: int,
        is_active: bool | None = True,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[Sequence[RecurringTodoRule], int]:
        base = select(RecurringTodoRule).where(RecurringTodoRule.workspace_id == workspace_id)
        if is_active is not None:
            base = base.where(RecurringTodoRule.is_active == is_active)
        total_q = select(func.count()).select_from(base.subquery())
        total = (await self.session.execute(total_q)).scalar_one()
        result = await self.session.execute(
            base.order_by(RecurringTodoRule.created_at.desc()).limit(limit).offset(offset)
        )
        return result.scalars().all(), total

    async def get_recurring_rule_by_public_id(
        self, workspace_id: int, public_id: UUID
    ) -> RecurringTodoRule | None:
        result = await self.session.execute(
            select(RecurringTodoRule).where(
                RecurringTodoRule.workspace_id == workspace_id,
                RecurringTodoRule.public_id == public_id,
            )
        )
        return result.scalar_one_or_none()

    async def create_recurring_rule(self, rule: RecurringTodoRule) -> RecurringTodoRule:
        self.session.add(rule)
        await self.session.flush()
        await self.session.refresh(rule)
        return rule

    async def save_recurring_rule(self, rule: RecurringTodoRule) -> RecurringTodoRule:
        self.session.add(rule)
        await self.session.flush()
        await self.session.refresh(rule)
        return rule

    async def delete_recurring_rule(self, rule: RecurringTodoRule) -> None:
        await self.session.delete(rule)
        await self.session.flush()
