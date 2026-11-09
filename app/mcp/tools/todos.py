"""Todo tools: create, read, update and summarise todos and recurring todos."""

from datetime import UTC, datetime
from typing import Any

from fastmcp import FastMCP

from app.core.database import postgres
from app.mcp import security
from app.mcp.support import common
from app.todo.repository import TodoRepository
from app.todo.schemas import TodoCreate, TodoResponse
from app.todo.service import TodoService


def register(mcp: FastMCP) -> None:
    """Register the todos tools on ``mcp``."""

    @mcp.tool
    async def create_todo(
        workspace_id: int, title: str, description: str | None = None, due_date: str | None = None
    ) -> dict[str, Any]:
        """Create a todo in a workspace the authenticated user belongs to."""
        async with postgres.async_session_maker() as session, session.begin():
            user_id = await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="create_todo"
            )
            due = datetime.fromisoformat(due_date) if due_date else None
            todo = await TodoService(TodoRepository(session)).create_todo(
                user_id,
                workspace_id,
                TodoCreate(title=title, description=description, due_date=due),
            )
            return TodoResponse.model_validate(todo).model_dump()

    @mcp.tool
    async def list_todos(
        workspace_id: int, completed: bool | None = None, limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        """List todos for an authorized workspace."""
        security.validate_page(limit, offset)
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="list_todos"
            )
            todos, total = await TodoService(TodoRepository(session)).list_todos(
                workspace_id, completed, limit, offset
            )
            return {
                "items": [TodoResponse.model_validate(t).model_dump() for t in todos],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    @mcp.tool
    async def get_todo_summary(workspace_id: int) -> dict[str, Any]:
        """Get todo counts and due items for an authorized workspace."""
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_todo_summary"
            )
            service = TodoService(TodoRepository(session))
            now = datetime.now(UTC)
            pending, completed = await service.get_summary_counts(workspace_id, now)
            overdue = await service.get_overdue_items(workspace_id, now, limit=5)
            next_due = await service.get_next_due_items(workspace_id, now, limit=5)
            return {
                "workspace_id": workspace_id,
                "pending": pending,
                "completed": completed,
                "overdue_count": len(overdue),
                "overdue_items": [TodoResponse.model_validate(t).model_dump() for t in overdue],
                "next_due_items": [TodoResponse.model_validate(t).model_dump() for t in next_due],
            }

    @mcp.tool
    async def create_todo_task(
        workspace_id: int,
        title: str,
        due_date: str | None = None,
        priority: str = "medium",
    ) -> dict[str, Any]:
        """Create a voice-compatible todo in an authorized workspace."""
        return await common.run_capture_tool(
            workspace_id,
            "mcp:write",
            "create_todo_task",
            {"title": title, "due_date": due_date, "priority": priority},
        )

    @mcp.tool
    async def create_recurring_todo(
        workspace_id: int,
        title: str,
        frequency: str = "weekly",
        interval: int = 1,
        due_time: str | None = None,
        timezone: str | None = None,
        end_date: str | None = None,
        monthly_mode: str = "day_of_month",
        by_weekday: int | None = None,
        by_ordinal: int | None = None,
        priority: str = "medium",
    ) -> dict[str, Any]:
        """Create a voice-compatible recurring todo in an authorized workspace."""
        return await common.run_capture_tool(
            workspace_id,
            "mcp:write",
            "create_recurring_todo",
            {
                "title": title,
                "frequency": frequency,
                "interval": interval,
                "due_time": due_time,
                "timezone": timezone,
                "end_date": end_date,
                "monthly_mode": monthly_mode,
                "by_weekday": by_weekday,
                "by_ordinal": by_ordinal,
                "priority": priority,
            },
        )

    @mcp.tool
    async def get_todo(workspace_id: int, public_id: str) -> dict[str, Any]:
        """Retrieve one todo in an authorized workspace."""
        return await common.run_capture_tool(
            workspace_id, "mcp:read", "get_todo", {"public_id": public_id}
        )

    @mcp.tool
    async def update_todo(
        workspace_id: int,
        public_id: str,
        title: str | None = None,
        description: str | None = None,
        due_date: str | None = None,
        priority: str | None = None,
        completed: bool | None = None,
    ) -> dict[str, Any]:
        """Update one todo in an authorized workspace."""
        return await common.run_capture_tool(
            workspace_id,
            "mcp:write",
            "update_todo",
            {
                "public_id": public_id,
                "title": title,
                "description": description,
                "due_date": due_date,
                "priority": priority,
                "completed": completed,
            },
        )

    @mcp.tool
    async def delete_todo(workspace_id: int, public_id: str) -> dict[str, Any]:
        """Delete one todo in an authorized workspace."""
        return await common.run_capture_tool(
            workspace_id, "mcp:write", "delete_todo", {"public_id": public_id}
        )

    @mcp.tool
    async def list_next_due_items(workspace_id: int, limit: int = 5) -> dict[str, Any]:
        """List the next due todos in an authorized workspace."""
        return await common.run_capture_tool(
            workspace_id, "mcp:read", "list_next_due_items", {"limit": limit}
        )
