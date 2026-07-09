from collections.abc import Sequence
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import case, func, select

from app.core.pagination import DEFAULT_LIMIT
from app.core.repository import BaseRepository
from app.todo.models import PriorityEnum, RecurringTodoRule, Todo


class TodoRepository(BaseRepository[Todo]):
    async def get_all(
        self,
        workspace_id: int,
        completed: bool | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        sort: Literal["created_at", "due_date"] = "created_at",
    ) -> tuple[Sequence[Todo], int]:
        base = select(Todo).where(Todo.workspace_id == workspace_id)
        if completed is not None:
            base = base.where(Todo.completed == completed)

        total_q = select(func.count()).select_from(base.subquery())
        total = (await self.session.execute(total_q)).scalar_one()

        if sort == "due_date":
            priority_rank = case(
                (Todo.priority == PriorityEnum.high.value, 0),
                (Todo.priority == PriorityEnum.medium.value, 1),
                (Todo.priority == PriorityEnum.low.value, 2),
                else_=3,
            )
            items_q = (
                base
                .order_by(
                    Todo.due_date.is_(None),
                    Todo.due_date.asc(),
                    priority_rank,
                    Todo.created_at.asc(),
                )
                .limit(limit)
                .offset(offset)
            )
        else:
            items_q = base.order_by(Todo.created_at.desc()).limit(limit).offset(offset)

        result = await self.session.execute(items_q)
        return result.scalars().all(), total

    async def get_subtask_counts(self, parent_ids: Sequence[int]) -> dict[int, int]:
        """Grouped count of children for a set of todo ids — one query
        regardless of page size, avoiding N+1 (spec-068)."""
        if not parent_ids:
            return {}
        result = await self.session.execute(
            select(Todo.parent_id, func.count(Todo.id))
            .where(Todo.parent_id.in_(parent_ids))
            .group_by(Todo.parent_id)
        )
        return dict(result.all())

    async def get_public_ids_by_ids(self, ids: Sequence[int]) -> dict[int, UUID]:
        if not ids:
            return {}
        result = await self.session.execute(select(Todo.id, Todo.public_id).where(Todo.id.in_(ids)))
        return dict(result.all())

    async def get_child_count(self, workspace_id: int, todo_id: int) -> int:
        result = await self.session.execute(
            select(func.count(Todo.id)).where(
                Todo.workspace_id == workspace_id, Todo.parent_id == todo_id
            )
        )
        return int(result.scalar() or 0)

    async def get_open_children(self, workspace_id: int, parent_id: int) -> Sequence[Todo]:
        result = await self.session.execute(
            select(Todo).where(
                Todo.workspace_id == workspace_id,
                Todo.parent_id == parent_id,
                Todo.completed.is_(False),
            )
        )
        return result.scalars().all()

    async def delete_completed(self, workspace_id: int) -> int:
        """Bulk-delete all completed todos in the workspace (Clear completed,
        spec-068). Returns the number of rows deleted."""
        todos = (
            (
                await self.session.execute(
                    select(Todo).where(Todo.workspace_id == workspace_id, Todo.completed.is_(True))
                )
            )
            .scalars()
            .all()
        )
        for todo in todos:
            await self.session.delete(todo)
        await self.session.flush()
        return len(todos)

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

    async def get_overdue_items(
        self, workspace_id: int, now: datetime, limit: int = 5
    ) -> Sequence[Todo]:
        """Incomplete overdue todos, oldest-due first — the morning briefing
        (spec-067) names the most-overdue item as the representative example."""
        result = await self.session.execute(
            select(Todo)
            .where(
                Todo.workspace_id == workspace_id,
                Todo.completed.is_(False),
                Todo.due_date.is_not(None),
                Todo.due_date < now,
            )
            .order_by(Todo.due_date.asc())
            .limit(limit)
        )
        return result.scalars().all()

    async def get_recurring_rules_due_between(
        self, workspace_id: int, start_date: date, end_date: date
    ) -> Sequence[RecurringTodoRule]:
        """Active recurring todo rules whose next occurrence falls in
        [start_date, end_date] — briefing "recurring due soon" line (spec-067)."""
        result = await self.session.execute(
            select(RecurringTodoRule)
            .where(
                RecurringTodoRule.workspace_id == workspace_id,
                RecurringTodoRule.is_active == True,  # noqa: E712
                RecurringTodoRule.next_due_date >= start_date,
                RecurringTodoRule.next_due_date <= end_date,
            )
            .order_by(RecurringTodoRule.next_due_date.asc())
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

    async def get_due_for_reminder(self, workspace_id: int, window_end: datetime) -> Sequence[Todo]:
        """Incomplete todos due within the look-ahead window that haven't
        already had a reminder notification created (spec-052)."""
        result = await self.session.execute(
            select(Todo).where(
                Todo.workspace_id == workspace_id,
                Todo.completed.is_(False),
                Todo.due_date.is_not(None),
                Todo.due_date <= window_end,
                Todo.reminded_at.is_(None),
            )
        )
        return result.scalars().all()

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
