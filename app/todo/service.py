import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.exceptions import NotFoundError
from app.core.pagination import DEFAULT_LIMIT
from app.todo.models import Todo
from app.todo.repository import TodoRepository
from app.todo.schemas import TodoCreate, TodoUpdate


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

    async def get_todo(self, workspace_id: int, public_id: uuid.UUID) -> Todo:
        todo = await self.repository.get_by_public_id(workspace_id, public_id)
        if not todo:
            raise NotFoundError(detail=f"Todo with id {public_id} not found")
        return todo

    async def create_todo(self, user_id: int, workspace_id: int, todo_in: TodoCreate) -> Todo:
        new_todo = Todo(user_id=user_id, workspace_id=workspace_id, **todo_in.model_dump())
        return await self.repository.create(new_todo)

    async def update_todo(
        self, workspace_id: int, public_id: uuid.UUID, todo_in: TodoUpdate
    ) -> Todo:
        todo = await self.get_todo(workspace_id, public_id)

        update_data = todo_in.model_dump(exclude_unset=True)
        if not update_data:
            return todo

        for key, value in update_data.items():
            setattr(todo, key, value)

        todo.updated_at = datetime.now(UTC)
        return await self.repository.save(todo)

    async def delete_todo(self, workspace_id: int, public_id: uuid.UUID) -> None:
        todo = await self.get_todo(workspace_id, public_id)
        await self.repository.delete(todo)
