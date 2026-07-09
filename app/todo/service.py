import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.audit import AuditLogger, snapshot_columns
from app.core.exceptions import NotFoundError, ValidationError
from app.core.pagination import DEFAULT_LIMIT
from app.core.recurrence import validate_recurrence_fields
from app.todo.models import PriorityEnum, RecurringTodoRule, Todo
from app.todo.repository import TodoRepository
from app.todo.schemas import (
    RecurringTodoRuleCreate,
    RecurringTodoRuleUpdate,
    TodoCreate,
    TodoResponse,
    TodoUpdate,
)

_TODO_AUDIT_FIELDS = (
    "title",
    "description",
    "due_date",
    "priority",
    "completed",
)

_RECURRING_RULE_AUDIT_FIELDS = (
    "title",
    "description",
    "priority",
    "frequency",
    "interval",
    "anchor_date",
    "due_time",
    "timezone",
    "next_due_date",
    "end_date",
    "is_active",
    "monthly_mode",
    "by_weekday",
    "by_ordinal",
)


def _snapshot_todo(todo: Todo) -> dict:
    data = snapshot_columns(todo, _TODO_AUDIT_FIELDS)
    # Convert date fields to ISO format for JSON serialization
    if data.get("due_date") is not None:
        data["due_date"] = data["due_date"].isoformat()
    return data


def _snapshot_recurring_rule(rule: RecurringTodoRule) -> dict:
    data = snapshot_columns(rule, _RECURRING_RULE_AUDIT_FIELDS)
    # Convert date/time fields to ISO format for JSON serialization
    for field in ("anchor_date", "due_time", "next_due_date", "end_date"):
        if data.get(field) is not None:
            data[field] = data[field].isoformat()
    return data


class TodoService:
    def __init__(self, repository: TodoRepository):
        self.repository = repository

    async def list_todos(
        self,
        workspace_id: int,
        completed: bool | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        sort: Literal["created_at", "due_date"] = "created_at",
    ) -> tuple[list[TodoResponse], int]:
        items, total = await self.repository.get_all(workspace_id, completed, limit, offset, sort)
        return await self._to_responses(items), total

    async def _to_responses(self, items: Sequence[Todo]) -> list[TodoResponse]:
        if not items:
            return []
        ids = [t.id for t in items]
        parent_ids = [t.parent_id for t in items if t.parent_id is not None]
        subtask_counts = await self.repository.get_subtask_counts(ids)
        parent_public_ids = await self.repository.get_public_ids_by_ids(parent_ids)
        return [
            self._build_response(
                t,
                subtask_count=subtask_counts.get(t.id, 0),
                parent_public_id=parent_public_ids.get(t.parent_id) if t.parent_id else None,
            )
            for t in items
        ]

    async def to_response(self, workspace_id: int, todo: Todo) -> TodoResponse:
        subtask_count = await self.repository.get_child_count(workspace_id, todo.id)
        parent_public_id = None
        if todo.parent_id is not None:
            ids_map = await self.repository.get_public_ids_by_ids([todo.parent_id])
            parent_public_id = ids_map.get(todo.parent_id)
        return self._build_response(
            todo, subtask_count=subtask_count, parent_public_id=parent_public_id
        )

    @staticmethod
    def _build_response(
        todo: Todo, *, subtask_count: int, parent_public_id: uuid.UUID | None
    ) -> TodoResponse:
        return TodoResponse(
            public_id=todo.public_id,
            title=todo.title,
            description=todo.description,
            due_date=todo.due_date,
            priority=todo.priority,
            completed=todo.completed,
            parent_public_id=parent_public_id,
            subtask_count=subtask_count,
            created_at=todo.created_at,
            updated_at=todo.updated_at,
        )

    async def get_summary_counts(self, workspace_id: int, now: datetime) -> tuple[int, int]:
        return await self.repository.get_summary_counts(workspace_id, now)

    async def get_next_due_items(
        self, workspace_id: int, now: datetime, limit: int = 5
    ) -> Sequence[Todo]:
        return await self.repository.get_next_due_items(workspace_id, now, limit)

    async def get_active_guardrail_todo_count(self, workspace_id: int) -> int:
        return await self.repository.get_active_guardrail_todo_count(workspace_id)

    async def get_overdue_items(
        self, workspace_id: int, now: datetime, limit: int = 5
    ) -> Sequence[Todo]:
        return await self.repository.get_overdue_items(workspace_id, now, limit)

    async def get_recurring_rules_due_between(
        self, workspace_id: int, start_date, end_date
    ) -> Sequence[RecurringTodoRule]:
        return await self.repository.get_recurring_rules_due_between(
            workspace_id, start_date, end_date
        )

    async def get_todo(self, workspace_id: int, public_id: uuid.UUID) -> Todo:
        todo = await self.repository.get_by_public_id(workspace_id, public_id)
        if not todo:
            raise NotFoundError(detail=f"Todo with id {public_id} not found")
        return todo

    async def get_todo_response(self, workspace_id: int, public_id: uuid.UUID) -> TodoResponse:
        todo = await self.get_todo(workspace_id, public_id)
        return await self.to_response(workspace_id, todo)

    async def _resolve_parent(
        self, workspace_id: int, parent_public_id: uuid.UUID, *, child: Todo | None = None
    ) -> Todo:
        parent = await self.repository.get_by_public_id(workspace_id, parent_public_id)
        if not parent:
            raise ValidationError(detail=f"Parent todo {parent_public_id} not found")
        if parent.parent_id is not None:
            raise ValidationError(detail="A subtask cannot itself be a parent (one level only)")
        if child is not None and parent.id == child.id:
            raise ValidationError(detail="A todo cannot be its own parent")
        return parent

    async def ensure_system_task(
        self,
        *,
        workspace_id: int,
        user_id: int,
        system_key: str,
        title: str,
        description: str,
        priority: PriorityEnum,
        existing_todo: Todo | None = None,
        audit_logger: AuditLogger | None = None,
        audit_module: str = "todo",
        audit_action: str = "update",
    ) -> tuple[Todo, Literal["created", "updated", "unchanged"]]:
        todo = existing_todo
        if todo is None:
            todo = Todo(
                workspace_id=workspace_id,
                user_id=user_id,
                title=title,
                description=description,
                priority=priority,
                system_key=system_key,
            )
            todo = await self.repository.create(todo)
            if audit_logger:
                after_snap = _snapshot_todo(todo)
                await audit_logger.log(
                    workspace_id=workspace_id,
                    actor_id=user_id,
                    action=audit_action,
                    module=audit_module,
                    entity_type="todo",
                    entity_id=todo.id,
                    details={
                        "entity_public_id": str(todo.public_id),
                        "before": None,
                        "after": after_snap,
                        "changed_fields": list(after_snap.keys()),
                    },
                )
            return todo, "created"

        before_snap = _snapshot_todo(todo)
        updated = False
        if todo.completed:
            todo.completed = False
            updated = True
        if todo.title != title:
            todo.title = title
            updated = True
        if todo.description != description:
            todo.description = description
            updated = True
        if todo.priority != priority:
            todo.priority = priority
            updated = True

        if not updated:
            return todo, "unchanged"

        todo.updated_at = datetime.now(UTC)
        todo = await self.repository.save(todo)

        if audit_logger:
            after_snap = _snapshot_todo(todo)
            changed_fields = [k for k in before_snap if before_snap[k] != after_snap[k]]
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action=audit_action,
                module=audit_module,
                entity_type="todo",
                entity_id=todo.id,
                details={
                    "entity_public_id": str(todo.public_id),
                    "before": before_snap,
                    "after": after_snap,
                    "changed_fields": changed_fields,
                },
            )
        return todo, "updated"

    async def create_todo(
        self,
        user_id: int,
        workspace_id: int,
        todo_in: TodoCreate,
        audit_logger: AuditLogger | None = None,
    ) -> Todo:
        data = todo_in.model_dump(exclude={"parent_public_id"})
        parent_id = None
        if todo_in.parent_public_id is not None:
            parent = await self._resolve_parent(workspace_id, todo_in.parent_public_id)
            parent_id = parent.id

        new_todo = Todo(user_id=user_id, workspace_id=workspace_id, parent_id=parent_id, **data)
        todo = await self.repository.create(new_todo)

        if audit_logger:
            after_snap = _snapshot_todo(todo)
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="create",
                module="todo",
                entity_type="todo",
                entity_id=todo.id,
                details={
                    "entity_public_id": str(todo.public_id),
                    "before": None,
                    "after": after_snap,
                    "changed_fields": list(after_snap.keys()),
                },
            )
        return todo

    async def update_todo(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        todo_in: TodoUpdate,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> Todo:
        todo = await self.get_todo(workspace_id, public_id)
        before_snap = _snapshot_todo(todo)

        update_data = todo_in.model_dump(exclude_unset=True)
        if not update_data:
            return todo

        reparent_set = "parent_public_id" in update_data
        new_parent_public_id = update_data.pop("parent_public_id", None)

        if reparent_set:
            if new_parent_public_id is None:
                todo.parent_id = None
            else:
                if await self.repository.get_child_count(workspace_id, todo.id) > 0:
                    raise ValidationError(
                        detail="A todo with subtasks cannot itself be made a subtask"
                    )
                parent = await self._resolve_parent(workspace_id, new_parent_public_id, child=todo)
                todo.parent_id = parent.id

        # A moved due_date re-arms the reminder (spec-052) — reset the dedup
        # marker so todo_reminder_job treats it as not-yet-reminded.
        if "due_date" in update_data and update_data["due_date"] != todo.due_date:
            todo.reminded_at = None

        for key, value in update_data.items():
            setattr(todo, key, value)

        todo.updated_at = datetime.now(UTC)
        todo = await self.repository.save(todo)

        after_snap = _snapshot_todo(todo)
        changed_fields = [k for k in before_snap if before_snap[k] != after_snap[k]]
        action = "update"
        if "completed" in changed_fields and after_snap["completed"] is True:
            action = "complete"

        if audit_logger and actor_id is not None:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action=action,
                module="todo",
                entity_type="todo",
                entity_id=todo.id,
                details={
                    "entity_public_id": str(todo.public_id),
                    "before": before_snap,
                    "after": after_snap,
                    "changed_fields": changed_fields,
                },
            )

        if action == "complete" and todo.parent_id is None:
            await self._cascade_complete_children(
                workspace_id, todo, actor_id=actor_id, audit_logger=audit_logger
            )

        return todo

    async def _cascade_complete_children(
        self,
        workspace_id: int,
        parent: Todo,
        *,
        actor_id: int | None,
        audit_logger: AuditLogger | None,
    ) -> None:
        """Completing a parent also completes its open subtasks (spec-068),
        each recorded as its own audit entry noting the cascade."""
        open_children = await self.repository.get_open_children(workspace_id, parent.id)
        for child in open_children:
            before_snap = _snapshot_todo(child)
            child.completed = True
            child.updated_at = datetime.now(UTC)
            child = await self.repository.save(child)
            if audit_logger and actor_id is not None:
                after_snap = _snapshot_todo(child)
                await audit_logger.log(
                    workspace_id=workspace_id,
                    actor_id=actor_id,
                    action="complete",
                    module="todo",
                    entity_type="todo",
                    entity_id=child.id,
                    details={
                        "entity_public_id": str(child.public_id),
                        "before": before_snap,
                        "after": after_snap,
                        "changed_fields": ["completed"],
                        "cascade_from_parent": str(parent.public_id),
                    },
                )

    async def delete_todo(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        todo = await self.get_todo(workspace_id, public_id)
        before_snap = _snapshot_todo(todo)
        subtask_count = await self.repository.get_child_count(workspace_id, todo.id)

        await self.repository.delete(todo)  # ON DELETE CASCADE removes subtasks at the DB level

        if audit_logger and actor_id is not None:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="delete",
                module="todo",
                entity_type="todo",
                entity_id=todo.id,
                details={
                    "entity_public_id": str(todo.public_id),
                    "before": before_snap,
                    "after": None,
                    "changed_fields": [],
                    "subtasks_deleted": subtask_count,
                },
            )

    async def delete_completed_todos(
        self,
        workspace_id: int,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> int:
        """Clear completed (spec-068) — bulk-delete all completed todos in the
        workspace, audit-logged as one entry with the count."""
        deleted = await self.repository.delete_completed(workspace_id)
        if audit_logger and actor_id is not None and deleted:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="delete",
                module="todo",
                entity_type="todo",
                entity_id=0,
                details={
                    "entity_public_id": "bulk:clear_completed",
                    "before": {"deleted_count": deleted},
                    "after": None,
                    "changed_fields": [],
                },
            )
        return deleted

    async def list_recurring_rules(
        self,
        workspace_id: int,
        is_active: bool | None = True,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> tuple[Sequence[RecurringTodoRule], int]:
        return await self.repository.get_recurring_rules(workspace_id, is_active, limit, offset)

    async def get_recurring_rule(
        self, workspace_id: int, public_id: uuid.UUID
    ) -> RecurringTodoRule:
        rule = await self.repository.get_recurring_rule_by_public_id(workspace_id, public_id)
        if not rule:
            raise NotFoundError(detail=f"Recurring todo rule with id {public_id} not found")
        return rule

    async def create_recurring_rule(
        self,
        user_id: int,
        workspace_id: int,
        rule_in: RecurringTodoRuleCreate,
        audit_logger: AuditLogger | None = None,
    ) -> RecurringTodoRule:
        next_due = rule_in.anchor_date
        try:
            ZoneInfo(rule_in.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValidationError(detail=f"Unknown timezone: {rule_in.timezone}") from exc
        if rule_in.end_date and next_due > rule_in.end_date:
            raise ValidationError(detail="anchor_date cannot be after end_date")
        rule = RecurringTodoRule(
            user_id=user_id,
            workspace_id=workspace_id,
            title=rule_in.title,
            description=rule_in.description,
            priority=rule_in.priority,
            frequency=rule_in.frequency,
            interval=rule_in.interval,
            anchor_date=rule_in.anchor_date,
            due_time=rule_in.due_time,
            timezone=rule_in.timezone,
            next_due_date=next_due,
            end_date=rule_in.end_date,
            monthly_mode=rule_in.monthly_mode,
            by_weekday=rule_in.by_weekday,
            by_ordinal=rule_in.by_ordinal,
        )
        rule = await self.repository.create_recurring_rule(rule)
        if audit_logger:
            after_snap = _snapshot_recurring_rule(rule)
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="create",
                module="todo",
                entity_type="recurring_todo_rule",
                entity_id=rule.id,
                details={
                    "entity_public_id": str(rule.public_id),
                    "before": None,
                    "after": after_snap,
                    "changed_fields": list(after_snap.keys()),
                },
            )
        return rule

    async def update_recurring_rule(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        rule_in: RecurringTodoRuleUpdate,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> RecurringTodoRule:
        rule = await self.get_recurring_rule(workspace_id, public_id)
        before_snap = _snapshot_recurring_rule(rule)
        update_data = rule_in.model_dump(exclude_unset=True)
        if not update_data:
            return rule

        for key, value in update_data.items():
            setattr(rule, key, value)

        if rule.timezone is None:
            raise ValidationError(detail="Timezone cannot be null")
        try:
            ZoneInfo(rule.timezone)
        except (ZoneInfoNotFoundError, TypeError) as exc:
            raise ValidationError(detail=f"Unknown timezone: {rule.timezone}") from exc
        if rule.end_date and rule.anchor_date > rule.end_date:
            raise ValidationError(detail="anchor_date cannot be after end_date")
        try:
            validate_recurrence_fields(
                rule.frequency, rule.monthly_mode, rule.by_weekday, rule.by_ordinal
            )
        except ValueError as exc:
            raise ValidationError(detail=str(exc)) from exc

        if rule.end_date and rule.next_due_date > rule.end_date:
            rule.is_active = False
        rule.updated_at = datetime.now(UTC)
        rule = await self.repository.save_recurring_rule(rule)

        if audit_logger and actor_id is not None:
            after_snap = _snapshot_recurring_rule(rule)
            changed_fields = [k for k in before_snap if before_snap[k] != after_snap[k]]
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="update",
                module="todo",
                entity_type="recurring_todo_rule",
                entity_id=rule.id,
                details={
                    "entity_public_id": str(rule.public_id),
                    "before": before_snap,
                    "after": after_snap,
                    "changed_fields": changed_fields,
                },
            )
        return rule

    async def delete_recurring_rule(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        rule = await self.get_recurring_rule(workspace_id, public_id)
        before_snap = _snapshot_recurring_rule(rule)
        await self.repository.delete_recurring_rule(rule)
        if audit_logger and actor_id is not None:
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="delete",
                module="todo",
                entity_type="recurring_todo_rule",
                entity_id=rule.id,
                details={
                    "entity_public_id": str(rule.public_id),
                    "before": before_snap,
                    "after": None,
                    "changed_fields": [],
                },
            )
