import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.auth.models import User
from app.config import settings
from app.core.audit import AuditLog
from app.core.database import postgres
from app.finance.models import Account, FxRate
from app.investing.models import CashBalance
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
async def test_cannot_add_member_to_inactive_workspace(client: AsyncClient):
    owner = await _register_and_login(client, "platforminactivews")
    target = await _register_and_login(client, "platforminactivetarget")

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(
                select(Workspace).where(Workspace.public_id == owner["workspace_public_id"])
            )
        ).scalar_one()
        workspace.is_active = False
        await session.commit()

    response = await client.post(
        f"/v1/platform/workspaces/{owner['workspace_public_id']}/members",
        json={"user_public_id": str(target["user_public_id"]), "role": WorkspaceRole.VIEWER.value},
        cookies=owner["cookies"],
    )

    assert response.status_code == 404
    assert "inactive" in response.json()["detail"]


@pytest.mark.asyncio
async def test_cannot_add_inactive_user_to_workspace(client: AsyncClient):
    owner = await _register_and_login(client, "platforminactiveowner")
    target = await _register_and_login(client, "platforminactiveuser")

    async with postgres.async_session_maker() as session:
        target_user = (
            await session.execute(select(User).where(User.public_id == target["user_public_id"]))
        ).scalar_one()
        target_user.is_active = False
        await session.commit()

    response = await client.post(
        f"/v1/platform/workspaces/{owner['workspace_public_id']}/members",
        json={"user_public_id": str(target["user_public_id"]), "role": WorkspaceRole.VIEWER.value},
        cookies=owner["cookies"],
    )

    assert response.status_code == 404
    assert "inactive" in response.json()["detail"]


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
async def test_select_workspace_then_refresh_preserves_selected_workspace(client: AsyncClient):
    user = await _register_and_login(client, "platformselectrefresh")

    async with postgres.async_session_maker() as session:
        workspace_b = Workspace(name="Refresh Workspace")
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

    refresh_response = await client.post("/v1/auth/refresh")
    assert refresh_response.status_code == 200, refresh_response.text

    create_response = await client.post("/v1/todo/", json={"title": "Workspace B after refresh"})
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


@pytest.mark.asyncio
async def test_workspace_demo_reset(client: AsyncClient):
    owner = await _register_and_login(client, "platformreset")

    create_response = await client.post(
        "/v1/todo/",
        json={"title": "Should be deleted"},
        cookies=owner["cookies"],
    )
    assert create_response.status_code == 201

    # 1. By default, ENABLE_DEMO_RESET is False, so reset is blocked (403)
    reset_blocked_resp = await client.post(
        f"/v1/platform/workspaces/{owner['workspace_public_id']}/reset-demo",
        cookies=owner["cookies"],
    )
    assert reset_blocked_resp.status_code == 403
    reset_status_resp = await client.get(
        f"/v1/platform/workspaces/{owner['workspace_public_id']}/reset-demo/status",
        cookies=owner["cookies"],
    )
    assert reset_status_resp.status_code == 200
    assert reset_status_resp.json()["allowed"] is False
    assert reset_status_resp.json()["reason"] == "feature_flag_disabled"
    async with postgres.async_session_maker() as session:
        blocked_audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.workspace_id == owner["workspace_id"],
                    AuditLog.action == "demo_reset_denied",
                )
            )
        ).scalar_one()
        assert blocked_audit.details["after"]["reason"] == "feature_flag_disabled"

    # Enable flag for the remainder of test
    settings.ENABLE_DEMO_RESET = True
    try:
        # 2. Check that a non-owner/non-admin user is rejected
        member = await _register_and_login(client, "platformresetmember")
        async with postgres.async_session_maker() as session:
            member_user = (
                await session.execute(
                    select(User).where(User.public_id == member["user_public_id"])
                )
            ).scalar_one()
            session.add(
                WorkspaceMembership(
                    workspace_id=owner["workspace_id"],
                    user_id=member_user.id,
                    role=WorkspaceRole.MEMBER,
                )
            )
            await session.commit()

        # Select workspace for member
        select_resp = await client.post(
            f"/v1/platform/workspaces/{owner['workspace_public_id']}/select",
            cookies=member["cookies"],
        )
        assert select_resp.status_code == 204

        member_status_resp = await client.get(
            f"/v1/platform/workspaces/{owner['workspace_public_id']}/reset-demo/status",
            cookies=dict(client.cookies),
        )
        assert member_status_resp.status_code == 200
        assert member_status_resp.json()["allowed"] is False
        assert member_status_resp.json()["reason"] == "insufficient_role"

        reset_member_resp = await client.post(
            f"/v1/platform/workspaces/{owner['workspace_public_id']}/reset-demo",
            cookies=dict(client.cookies),
        )
        assert reset_member_resp.status_code == 403

        # 3. Reset successfully as owner
        owner_status_resp = await client.get(
            f"/v1/platform/workspaces/{owner['workspace_public_id']}/reset-demo/status",
            cookies=owner["cookies"],
        )
        assert owner_status_resp.status_code == 200
        assert owner_status_resp.json()["allowed"] is True
        assert owner_status_resp.json()["workspace_name"] == f"{owner['username']}'s Workspace"

        reset_resp = await client.post(
            f"/v1/platform/workspaces/{owner['workspace_public_id']}/reset-demo",
            cookies=owner["cookies"],
        )
        assert reset_resp.status_code == 200
        assert reset_resp.json() == {"status": "reset_success"}

        todos_resp = await client.get("/v1/todo/", cookies=owner["cookies"])
        assert todos_resp.status_code == 200
        todo_items = todos_resp.json()["items"]
        assert len(todo_items) == 2
        assert "Should be deleted" not in [t["title"] for t in todo_items]
        assert "buy groceries tomorrow" in [t["title"] for t in todo_items]

        async with postgres.async_session_maker() as session:
            reset_audit = (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.workspace_id == owner["workspace_id"],
                        AuditLog.action == "demo_reset",
                    )
                )
            ).scalar_one()
            assert reset_audit.details["after"]["fixture_version"] == "2026-06-10"
            assert reset_audit.details["after"]["seeded"]["accounts"] == [
                "brokerage",
                "wallet",
                "eur-wallet",
            ]

            accounts = (
                (
                    await session.execute(
                        select(Account)
                        .where(Account.workspace_id == owner["workspace_id"])
                        .order_by(Account.name.asc())
                    )
                )
                .scalars()
                .all()
            )
            assert [(account.name, account.default_currency_code) for account in accounts] == [
                ("brokerage", "USD"),
                ("eur-wallet", "EUR"),
                ("wallet", "USD"),
            ]

            eur_cash = (
                await session.execute(
                    select(CashBalance).where(
                        CashBalance.workspace_id == owner["workspace_id"],
                        CashBalance.currency == "EUR",
                    )
                )
            ).scalar_one()
            assert eur_cash.balance == Decimal("1200.00")

            eur_usd = (
                await session.execute(
                    select(FxRate).where(
                        FxRate.base_currency_code == "EUR",
                        FxRate.quote_currency_code == "USD",
                        FxRate.source == "ECB",
                    )
                )
            ).scalar_one()
            assert eur_usd.rate == Decimal("1.0850000000")
    finally:
        settings.ENABLE_DEMO_RESET = False


@pytest.mark.asyncio
async def test_workspace_demo_reset_requires_active_workspace(client: AsyncClient):
    owner = await _register_and_login(client, "platformresetactive")

    async with postgres.async_session_maker() as session:
        workspace_b = Workspace(name="Reset Target B")
        session.add(workspace_b)
        await session.flush()
        await session.refresh(workspace_b)
        user_row = (
            await session.execute(select(User).where(User.public_id == owner["user_public_id"]))
        ).scalar_one()
        session.add(
            WorkspaceMembership(
                workspace_id=workspace_b.id,
                user_id=user_row.id,
                role=WorkspaceRole.OWNER,
            )
        )
        await session.commit()
        workspace_b_public_id = workspace_b.public_id
        workspace_b_id = workspace_b.id

    settings.ENABLE_DEMO_RESET = True
    try:
        reset_inactive_resp = await client.post(
            f"/v1/platform/workspaces/{workspace_b_public_id}/reset-demo",
            cookies=owner["cookies"],
        )
        assert reset_inactive_resp.status_code == 403
        assert "active workspace" in reset_inactive_resp.json()["detail"]

        inactive_status_resp = await client.get(
            f"/v1/platform/workspaces/{workspace_b_public_id}/reset-demo/status",
            cookies=owner["cookies"],
        )
        assert inactive_status_resp.status_code == 200
        assert inactive_status_resp.json()["allowed"] is False
        assert inactive_status_resp.json()["reason"] == "workspace_not_active"

        async with postgres.async_session_maker() as session:
            denied_audit = (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.workspace_id == workspace_b_id,
                        AuditLog.action == "demo_reset_denied",
                    )
                )
            ).scalar_one()
            assert denied_audit.details["after"]["reason"] == "workspace_not_active"

        select_resp = await client.post(
            f"/v1/platform/workspaces/{workspace_b_public_id}/select",
            cookies=owner["cookies"],
        )
        assert select_resp.status_code == 204

        reset_active_resp = await client.post(
            f"/v1/platform/workspaces/{workspace_b_public_id}/reset-demo",
            cookies=dict(client.cookies),
        )
        assert reset_active_resp.status_code == 200
        assert reset_active_resp.json() == {"status": "reset_success"}
    finally:
        settings.ENABLE_DEMO_RESET = False
