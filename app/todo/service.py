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
    ) -> tuple[Sequence[Todo], int]:
        return await self.repository.get_all(workspace_id, completed, limit, offset)

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
        new_todo = Todo(user_id=user_id, workspace_id=workspace_id, **todo_in.model_dump())
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

        # A moved due_date re-arms the reminder (spec-052) — reset the dedup
        # marker so todo_reminder_job treats it as not-yet-reminded.
        if "due_date" in update_data and update_data["due_date"] != todo.due_date:
            todo.reminded_at = None

        for key, value in update_data.items():
            setattr(todo, key, value)

        todo.updated_at = datetime.now(UTC)
        todo = await self.repository.save(todo)

        if audit_logger and actor_id is not None:
            after_snap = _snapshot_todo(todo)
            changed_fields = [k for k in before_snap if before_snap[k] != after_snap[k]]

            action = "update"
            if "completed" in changed_fields and after_snap["completed"] is True:
                action = "complete"

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
        return todo

    async def delete_todo(
        self,
        workspace_id: int,
        public_id: uuid.UUID,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        todo = await self.get_todo(workspace_id, public_id)
        before_snap = _snapshot_todo(todo)

        await self.repository.delete(todo)

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
                },
            )

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
