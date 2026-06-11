from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.application.workflows import process_workspace_recurring_todos
from app.auth.models import User
from app.core.database import postgres
from app.platform.models import Workspace, WorkspaceMembership
from app.todo.models import RecurringTodoRule, Todo


@pytest.mark.asyncio
async def test_process_workspace_recurring_todos_generates_items(override_database_url):
    today = datetime.now(UTC).date()
    async with postgres.async_session_maker() as session:
        user = User(
            id=808,
            email="recur-todo-owner@example.com",
            username="recurtodoowner",
            hashed_password="hashed",
        )
        workspace = Workspace(id=1808, name="Recurring Todo Workspace")
        session.add(user)
        session.add(workspace)
        await session.flush()
        session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="owner"))
        session.add(
            RecurringTodoRule(
                workspace_id=workspace.id,
                user_id=user.id,
                title="Weekly review",
                description="Review weekly plan",
                priority="medium",
                frequency="weekly",
                interval=1,
                anchor_date=today,
                next_due_date=today,
                is_active=True,
            )
        )
        await session.commit()

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == 1808))
        ).scalar_one()
        count = await process_workspace_recurring_todos(session, workspace)
        await session.commit()
        assert count == 1

    async with postgres.async_session_maker() as session:
        todos = (
            (
                await session.execute(
                    select(Todo).where(Todo.workspace_id == 1808, Todo.title == "Weekly review")
                )
            )
            .scalars()
            .all()
        )
        assert len(todos) == 1
        assert todos[0].due_date is not None
        assert todos[0].due_date.date() <= datetime.now(UTC).date()
