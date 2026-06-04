import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.auth.models import User
from app.core.database import postgres
from app.platform.models import Workspace, WorkspaceMembership, WorkspaceRole
from app.todo.models import Todo


async def _register_and_login(client: AsyncClient, suffix: str) -> dict:
    username = f"{suffix}_{uuid.uuid4().hex[:6]}"
    email = f"{username}@example.com"
    password = "TestPass123!"

    reg = await client.post(
        "/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    assert reg.status_code == 200, reg.text

    login = await client.post(
        "/v1/auth/login",
        data={"username": username, "password": password},
    )
    assert login.status_code == 200, login.text

    async with postgres.async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.username == username))
        user = user_result.scalar_one()

        workspace_result = await session.execute(
            select(Workspace)
            .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
            .where(WorkspaceMembership.user_id == user.id)
        )
        workspace = workspace_result.scalar_one()

    return {
        "username": username,
        "password": password,
        "user_public_id": user.public_id,
        "workspace_public_id": workspace.public_id,
        "workspace_id": workspace.id,
        "cookies": dict(login.cookies),
    }


@pytest.mark.asyncio
async def test_list_workspaces_includes_real_name_and_role(client: AsyncClient):
    owner = await _register_and_login(client, "platformlist")

    response = await client.get("/v1/platform/workspaces/", cookies=owner["cookies"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"]
    assert body["items"][0]["public_id"] == str(owner["workspace_public_id"])
    assert body["items"][0]["name"] == f"{owner['username']}'s Workspace"
    assert body["items"][0]["role"] == WorkspaceRole.OWNER.value
    assert body["items"][0]["is_active"] is True


@pytest.mark.asyncio
async def test_owner_can_add_workspace_member(client: AsyncClient):
    owner = await _register_and_login(client, "platformowner")
    target = await _register_and_login(client, "platformtarget")

    response = await client.post(
        f"/v1/platform/workspaces/{owner['workspace_public_id']}/members",
        json={"user_public_id": str(target["user_public_id"]), "role": WorkspaceRole.VIEWER.value},
        cookies=owner["cookies"],
    )

    assert response.status_code == 201, response.text
    assert response.json() == {"status": "invited"}

    async with postgres.async_session_maker() as session:
        target_user = (
            await session.execute(select(User).where(User.public_id == target["user_public_id"]))
        ).scalar_one()
        membership = (
            await session.execute(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == owner["workspace_id"],
                    WorkspaceMembership.user_id == target_user.id,
                )
            )
        ).scalar_one()

    assert membership.role == WorkspaceRole.VIEWER


@pytest.mark.asyncio
async def test_member_cannot_add_workspace_member(client: AsyncClient):
    owner = await _register_and_login(client, "platformmemberowner")
    member = await _register_and_login(client, "platformmember")
    target = await _register_and_login(client, "platformtarget2")

    async with postgres.async_session_maker() as session:
        member_user = (
            await session.execute(select(User).where(User.public_id == member["user_public_id"]))
        ).scalar_one()
        session.add(
            WorkspaceMembership(
                workspace_id=owner["workspace_id"],
                user_id=member_user.id,
                role=WorkspaceRole.MEMBER,
            )
        )
        await session.commit()

    select_response = await client.post(
        f"/v1/platform/workspaces/{owner['workspace_public_id']}/select",
        cookies=member["cookies"],
    )
    assert select_response.status_code == 204, select_response.text
    member_cookies = dict(client.cookies)

    response = await client.post(
        f"/v1/platform/workspaces/{owner['workspace_public_id']}/members",
        json={"user_public_id": str(target["user_public_id"]), "role": WorkspaceRole.VIEWER.value},
        cookies=member_cookies,
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_select_workspace_changes_workspace_used_by_following_requests(client: AsyncClient):
    user = await _register_and_login(client, "platformselect")

    async with postgres.async_session_maker() as session:
        workspace_b = Workspace(name="Second Workspace")
        session.add(workspace_b)
        await session.flush()
        await session.refresh(workspace_b)
        user_row = (
            await session.execute(select(User).where(User.public_id == user["user_public_id"]))
        ).scalar_one()
        session.add(
            WorkspaceMembership(
                workspace_id=workspace_b.id,
                user_id=user_row.id,
                role=WorkspaceRole.MEMBER,
            )
        )
        await session.commit()
        workspace_b_public_id = workspace_b.public_id
        workspace_b_id = workspace_b.id

    select_response = await client.post(
        f"/v1/platform/workspaces/{workspace_b_public_id}/select",
        cookies=user["cookies"],
    )
    assert select_response.status_code == 204, select_response.text

    create_response = await client.post("/v1/todo/", json={"title": "Workspace B todo"})

    assert create_response.status_code == 201, create_response.text
    todo_public_id = uuid.UUID(create_response.json()["public_id"])

    async with postgres.async_session_maker() as session:
        todo = (
            await session.execute(select(Todo).where(Todo.public_id == todo_public_id))
        ).scalar_one()

    assert todo.workspace_id == workspace_b_id


@pytest.mark.asyncio
async def test_select_workspace_rejects_non_member(client: AsyncClient):
    owner = await _register_and_login(client, "platformrejectowner")
    other = await _register_and_login(client, "platformrejectother")

    response = await client.post(
        f"/v1/platform/workspaces/{owner['workspace_public_id']}/select",
        cookies=other["cookies"],
    )

    assert response.status_code == 403
