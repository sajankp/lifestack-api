import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.auth.models import User
from app.core.database import postgres
from app.platform.models import Workspace, WorkspaceMembership, WorkspaceRole
from app.todo.models import Todo


@pytest.mark.asyncio
async def test_workspace_resolution_logic(client: AsyncClient):
    # 1. Register User A
    username = f"user_{uuid.uuid4().hex[:8]}"
    email = f"{username}@example.com"
    await client.post(
        "/v1/auth/register",
        json={"email": email, "username": username, "password": "password123"},
    )

    # Login as User A to set cookies
    await client.post("/v1/auth/login", data={"username": username, "password": "password123"})

    # 2. Get User A's default workspace ID from DB
    async with postgres.async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.username == username))
        user_a = user_result.scalar_one()

        ws_result = await session.execute(
            select(Workspace)
            .join(WorkspaceMembership)
            .where(WorkspaceMembership.user_id == user_a.id)
        )
        workspace_a = ws_result.scalar_one()
        workspace_a_id = workspace_a.id

    # 3. Create another workspace B and membership for User A
    async with postgres.async_session_maker() as session:
        workspace_b = Workspace(name="Workspace B")
        session.add(workspace_b)
        await session.flush()
        await session.refresh(workspace_b)

        membership = WorkspaceMembership(
            workspace_id=workspace_b.id, user_id=user_a.id, role=WorkspaceRole.MEMBER
        )
        session.add(membership)
        await session.commit()
        workspace_b_id = workspace_b.id

    # 4. Successive calls to the same endpoint should resolve the same workspace
    # (Stage 1 resolution picks the first one found)
    create_response = await client.post("/v1/todo/", json={"title": "Test Todo"})
    assert create_response.status_code == 201
    todo_id = create_response.json()["public_id"]

    async with postgres.async_session_maker() as session:
        todo_result = await session.execute(
            select(Todo).where(Todo.public_id == uuid.UUID(todo_id))
        )
        todo = todo_result.scalar_one()
        # It should match the first workspace (workspace_a_id)
        assert todo.workspace_id == workspace_a_id

    # 5. Now delete membership for workspace A
    async with postgres.async_session_maker() as session:
        membership_a_result = await session.execute(
            select(WorkspaceMembership)
            .where(WorkspaceMembership.user_id == user_a.id)
            .where(WorkspaceMembership.workspace_id == workspace_a_id)
        )
        membership_a = membership_a_result.scalar_one()
        await session.delete(membership_a)
        await session.commit()

    # 6. Next request should resolve to workspace B, proving it uses membership logic
    create_response_2 = await client.post("/v1/todo/", json={"title": "Test Todo 2"})
    assert create_response_2.status_code == 201
    todo_id_2 = create_response_2.json()["public_id"]

    async with postgres.async_session_maker() as session:
        todo_result_2 = await session.execute(
            select(Todo).where(Todo.public_id == uuid.UUID(todo_id_2))
        )
        todo_2 = todo_result_2.scalar_one()
        assert todo_2.workspace_id == workspace_b_id
        assert todo_2.workspace_id != workspace_a_id
