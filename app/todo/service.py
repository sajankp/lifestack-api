import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.audit import AuditLogger
from app.core.exceptions import NotFoundError
from app.core.pagination import DEFAULT_LIMIT
from app.todo.models import Todo
from app.todo.repository import TodoRepository
from app.todo.schemas import TodoCreate, TodoUpdate


def _snapshot_todo(todo: Todo) -> dict:
    return {
        "title": todo.title,
        "description": todo.description,
        "due_date": todo.due_date.isoformat() if todo.due_date else None,
        "priority": todo.priority,
        "completed": todo.completed,
    }


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

    async def get_todo(self, workspace_id: int, public_id: uuid.UUID) -> Todo:
        todo = await self.repository.get_by_public_id(workspace_id, public_id)
        if not todo:
            raise NotFoundError(detail=f"Todo with id {public_id} not found")
        return todo

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
