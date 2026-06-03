import pytest
from httpx import AsyncClient

from app.auth.repository import UserRepository
from app.core.database import postgres
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService
from app.platform.repository import WorkspaceRepository
from app.tests.integration.test_spending import _register_and_login


@pytest.mark.asyncio
async def test_notification_service_and_endpoints(client: AsyncClient):
    # Register and log in user
    creds = await _register_and_login(client, "notifytest")
    cookies = creds["cookies"]

    async_session_maker = postgres.get_session_maker(postgres.engine)
    async with async_session_maker() as session:
        # Fetch workspace and user IDs from DB to use with service directly
        user_repo = UserRepository(session)
        user = await user_repo.get_by_username(creds["username"])
        assert user is not None
        user_id = user.id

        workspace_repo = WorkspaceRepository(session)
        workspaces = await workspace_repo.list_user_workspaces(user_id)
        workspace_id = workspaces[0].id

        # Instantiate notification service
        repo = NotificationRepository(session)
        service = NotificationService(repo)

        # 1. Test creating notification when not muted
        n1 = await service.notify(
            workspace_id=workspace_id,
            user_id=user_id,
            category="system",
            severity="info",
            title="Test notification 1",
            body="This is a test notification body",
        )
        assert n1 is not None
        assert n1.title == "Test notification 1"
        assert n1.workspace_id == workspace_id
        await session.commit()

    # 2. Test list notifications endpoint
    list_resp = await client.get("/v1/notifications", cookies=cookies)
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "Test notification 1"
    notification_id = items[0]["public_id"]

    # 3. Test unread count endpoint
    unread_resp = await client.get("/v1/notifications/unread-count", cookies=cookies)
    assert unread_resp.status_code == 200
    assert unread_resp.json()["count"] == 1

    # 4. Test mark read endpoint
    read_resp = await client.patch(f"/v1/notifications/{notification_id}/read", cookies=cookies)
    assert read_resp.status_code == 200
    assert read_resp.json()["is_read"] is True

    unread_resp = await client.get("/v1/notifications/unread-count", cookies=cookies)
    assert unread_resp.json()["count"] == 0

    async with async_session_maker() as session:
        # Instantiate notification service
        repo = NotificationRepository(session)
        service = NotificationService(repo)

        # 5. Test muting category preferences
        # Mute "system" category
        await service.update_preference(workspace_id, user_id, "system", {"is_muted": True})
        await session.commit()

        # Try to send a notification in "system" category - should return None
        n2 = await service.notify(
            workspace_id=workspace_id,
            user_id=user_id,
            category="system",
            severity="info",
            title="Should not be created",
        )
        assert n2 is None
        await session.commit()

    # Verify that no new notification was added
    list_resp = await client.get("/v1/notifications", cookies=cookies)
    assert len(list_resp.json()["items"]) == 1

    # 6. Test updating preference via PATCH endpoint
    pref_resp = await client.patch(
        "/v1/notifications/preferences/system", json={"is_muted": False}, cookies=cookies
    )
    assert pref_resp.status_code == 200
    assert pref_resp.json()["is_muted"] is False

    # 7. Test get preferences endpoint
    prefs_resp = await client.get("/v1/notifications/preferences", cookies=cookies)
    assert prefs_resp.status_code == 200
    prefs = prefs_resp.json()
    system_pref = next(p for p in prefs if p["category"] == "system")
    assert system_pref["is_muted"] is False

    # 8. Test mark all read
    # Create two unread notifications first
    async with async_session_maker() as session:
        repo = NotificationRepository(session)
        service = NotificationService(repo)
        await service.notify(workspace_id, user_id, "system", "info", "Unread 1")
        await service.notify(workspace_id, user_id, "system", "info", "Unread 2")
        await session.commit()

    unread_resp = await client.get("/v1/notifications/unread-count", cookies=cookies)
    assert unread_resp.json()["count"] == 2

    mark_all_resp = await client.post("/v1/notifications/mark-all-read", cookies=cookies)
    assert mark_all_resp.status_code == 200
    assert mark_all_resp.json()["updated"] == 2

    unread_resp = await client.get("/v1/notifications/unread-count", cookies=cookies)
    assert unread_resp.json()["count"] == 0

    # 9. Test dismiss endpoint
    list_resp = await client.get("/v1/notifications", cookies=cookies)
    n_id = list_resp.json()["items"][0]["public_id"]
    dismiss_resp = await client.delete(f"/v1/notifications/{n_id}", cookies=cookies)
    assert dismiss_resp.status_code == 204

    # Verify count decreased by 1
    list_resp = await client.get("/v1/notifications", cookies=cookies)
    assert len(list_resp.json()["items"]) == 2
