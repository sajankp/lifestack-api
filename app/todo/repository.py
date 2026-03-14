from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.todo.models import Todo


class TodoRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_all(self, workspace_id: int, completed: bool | None = None) -> Sequence[Todo]:
        query = select(Todo).where(Todo.workspace_id == workspace_id)
        if completed is not None:
            query = query.where(Todo.completed == completed)

        result = await self.session.execute(query)
        return result.scalars().all()

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
