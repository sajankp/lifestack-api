import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.application.jobs import push_delivery_job, todo_reminder_job
from app.auth.repository import UserRepository
from app.config import settings
from app.core.database import postgres
from app.notifications.models import Notification, NotificationDelivery, PushSubscription
from app.notifications.push import PushResult
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService
from app.platform.repository import WorkspaceRepository
from app.tests.integration.test_spending import _register_and_login
from app.todo.models import Todo


@pytest.fixture(autouse=True)
def _vapid_configured():
    original = (settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY, settings.VAPID_SUBJECT)
    settings.VAPID_PUBLIC_KEY = "test-public-key"
    settings.VAPID_PRIVATE_KEY = "test-private-key"
    settings.VAPID_SUBJECT = "mailto:test@example.com"
    yield
    settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY, settings.VAPID_SUBJECT = original


async def _workspace_and_user_id(creds: dict) -> tuple[int, int]:
    async with postgres.async_session_maker() as session:
        user = await UserRepository(session).get_by_username(creds["username"])
        assert user is not None
        workspaces = await WorkspaceRepository(session).list_user_workspaces(user.id)
        return workspaces[0].id, user.id


def _subscription_payload(endpoint: str) -> dict:
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": "test-p256dh-key", "auth": "test-auth-secret"},
        "device_label": "Chrome on Test",
    }


@pytest.mark.asyncio
async def test_push_subscription_lifecycle(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    cookies = creds["cookies"]
    endpoint = f"https://push.example.com/{uuid.uuid4().hex}"

    create_res = await client.post(
        "/v1/notifications/push-subscriptions",
        json=_subscription_payload(endpoint),
        cookies=cookies,
    )
    assert create_res.status_code == 201, create_res.text
    first_public_id = create_res.json()["public_id"]
    assert create_res.json()["endpoint_hint"].startswith("...")

    # Re-subscribing the same endpoint upserts — no duplicate row.
    resubscribe_res = await client.post(
        "/v1/notifications/push-subscriptions",
        json=_subscription_payload(endpoint),
        cookies=cookies,
    )
    assert resubscribe_res.status_code == 201, resubscribe_res.text
    assert resubscribe_res.json()["public_id"] == first_public_id

    list_res = await client.get("/v1/notifications/push-subscriptions", cookies=cookies)
    assert list_res.status_code == 200
    assert len(list_res.json()) == 1

    # A second workspace's user cannot see or delete it.
    other_creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    other_list_res = await client.get(
        "/v1/notifications/push-subscriptions", cookies=other_creds["cookies"]
    )
    assert other_list_res.json() == []
    other_delete_res = await client.delete(
        f"/v1/notifications/push-subscriptions/{first_public_id}", cookies=other_creds["cookies"]
    )
    assert other_delete_res.status_code == 404

    delete_res = await client.delete(
        f"/v1/notifications/push-subscriptions/{first_public_id}", cookies=cookies
    )
    assert delete_res.status_code == 204

    list_after_delete = await client.get("/v1/notifications/push-subscriptions", cookies=cookies)
    assert list_after_delete.json() == []


@pytest.mark.asyncio
async def test_notify_enqueues_push_only_with_preference_and_subscription(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    cookies = creds["cookies"]
    workspace_id, user_id = await _workspace_and_user_id(creds)

    # No preference row at all (default channel_push=False) and no subscription.
    async with postgres.async_session_maker() as session:
        service = NotificationService(NotificationRepository(session))
        n1 = await service.notify(
            workspace_id=workspace_id,
            user_id=user_id,
            category="todo_reminder",
            severity="info",
            title="No push yet",
        )
        await session.flush()
        deliveries = (
            (
                await session.execute(
                    select(NotificationDelivery).where(
                        NotificationDelivery.notification_id == n1.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [d.channel for d in deliveries] == ["in_app"]
        await session.commit()

    # Opt in to push for this category, then subscribe.
    pref_res = await client.patch(
        "/v1/notifications/preferences/todo_reminder",
        json={"channel_push": True},
        cookies=cookies,
    )
    assert pref_res.status_code == 200, pref_res.text
    sub_res = await client.post(
        "/v1/notifications/push-subscriptions",
        json=_subscription_payload(f"https://push.example.com/{uuid.uuid4().hex}"),
        cookies=cookies,
    )
    assert sub_res.status_code == 201, sub_res.text

    async with postgres.async_session_maker() as session:
        service = NotificationService(NotificationRepository(session))
        n2 = await service.notify(
            workspace_id=workspace_id,
            user_id=user_id,
            category="todo_reminder",
            severity="info",
            title="Now with push",
        )
        await session.flush()
        deliveries2 = (
            (
                await session.execute(
                    select(NotificationDelivery).where(
                        NotificationDelivery.notification_id == n2.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert sorted(d.channel for d in deliveries2) == ["in_app", "push"]
        push_delivery = next(d for d in deliveries2 if d.channel == "push")
        assert push_delivery.status == "pending"


@pytest.mark.asyncio
async def test_push_delivery_job_sends_and_deactivates_gone_subscriptions(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    cookies = creds["cookies"]
    workspace_id, user_id = await _workspace_and_user_id(creds)

    await client.patch(
        "/v1/notifications/preferences/todo_reminder",
        json={"channel_push": True},
        cookies=cookies,
    )
    await client.post(
        "/v1/notifications/push-subscriptions",
        json=_subscription_payload(f"https://push.example.com/{uuid.uuid4().hex}"),
        cookies=cookies,
    )

    async with postgres.async_session_maker() as session:
        service = NotificationService(NotificationRepository(session))
        await service.notify(
            workspace_id=workspace_id,
            user_id=user_id,
            category="todo_reminder",
            severity="info",
            title="Deliver me",
        )
        await session.commit()

    with patch(
        "app.application.workflows.send_web_push",
        return_value=PushResult(success=True),
    ) as mock_send:
        await push_delivery_job()
    mock_send.assert_called_once()

    async with postgres.async_session_maker() as session:
        deliveries = (
            (
                await session.execute(
                    select(NotificationDelivery).where(NotificationDelivery.channel == "push")
                )
            )
            .scalars()
            .all()
        )
        assert [d.status for d in deliveries] == ["sent"]

    # Re-run: nothing pending left, mock must not be called again.
    with patch(
        "app.application.workflows.send_web_push",
        return_value=PushResult(success=True),
    ) as mock_send_again:
        await push_delivery_job()
    mock_send_again.assert_not_called()

    # A second notification hitting a now-410-gone endpoint deactivates the subscription.
    async with postgres.async_session_maker() as session:
        service = NotificationService(NotificationRepository(session))
        await service.notify(
            workspace_id=workspace_id,
            user_id=user_id,
            category="todo_reminder",
            severity="info",
            title="Gone endpoint",
        )
        await session.commit()

    with patch(
        "app.application.workflows.send_web_push",
        return_value=PushResult(success=False, gone=True, error_detail="410 Gone"),
    ):
        await push_delivery_job()

    async with postgres.async_session_maker() as session:
        subscriptions = (await session.execute(select(PushSubscription))).scalars().all()
        assert len(subscriptions) == 1
        assert subscriptions[0].is_active is False

        failed_deliveries = (
            (
                await session.execute(
                    select(NotificationDelivery).where(
                        NotificationDelivery.channel == "push",
                        NotificationDelivery.status == "failed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(failed_deliveries) == 1


@pytest.mark.asyncio
async def test_todo_reminder_job_dedupes_and_rearms_on_due_date_change(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    cookies = creds["cookies"]
    workspace_id, _user_id = await _workspace_and_user_id(creds)

    due_soon = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    create_res = await client.post(
        "/v1/todo/", json={"title": "Take medication", "due_date": due_soon}, cookies=cookies
    )
    assert create_res.status_code == 201, create_res.text
    todo_public_id = create_res.json()["public_id"]

    completed_due_soon = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    completed_res = await client.post(
        "/v1/todo/",
        json={"title": "Already done", "due_date": completed_due_soon},
        cookies=cookies,
    )
    await client.patch(
        f"/v1/todo/{completed_res.json()['public_id']}",
        json={"completed": True},
        cookies=cookies,
    )

    await todo_reminder_job(workspace_id=workspace_id)

    async with postgres.async_session_maker() as session:
        notifications = (
            (
                await session.execute(
                    select(Notification).where(
                        Notification.workspace_id == workspace_id,
                        Notification.category == "todo_reminder",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(notifications) == 1
        assert notifications[0].title == "Reminder: Take medication"

        todo_row = (
            await session.execute(select(Todo).where(Todo.public_id == uuid.UUID(todo_public_id)))
        ).scalar_one()
        assert todo_row.reminded_at is not None

    # Re-run: reminded_at already set, no duplicate.
    await todo_reminder_job(workspace_id=workspace_id)
    async with postgres.async_session_maker() as session:
        count = (
            (
                await session.execute(
                    select(Notification).where(
                        Notification.workspace_id == workspace_id,
                        Notification.category == "todo_reminder",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(count) == 1

    # Moving due_date later re-arms the reminder.
    later_due = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
    patch_res = await client.patch(
        f"/v1/todo/{todo_public_id}", json={"due_date": later_due}, cookies=cookies
    )
    assert patch_res.status_code == 200, patch_res.text

    async with postgres.async_session_maker() as session:
        todo_row = (
            await session.execute(select(Todo).where(Todo.public_id == uuid.UUID(todo_public_id)))
        ).scalar_one()
        assert todo_row.reminded_at is None

    await todo_reminder_job(workspace_id=workspace_id)
    async with postgres.async_session_maker() as session:
        count2 = (
            (
                await session.execute(
                    select(Notification).where(
                        Notification.workspace_id == workspace_id,
                        Notification.category == "todo_reminder",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(count2) == 2


@pytest.mark.asyncio
async def test_push_endpoints_503_when_vapid_unconfigured(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    cookies = creds["cookies"]

    settings.VAPID_PUBLIC_KEY = None
    settings.VAPID_PRIVATE_KEY = None
    try:
        key_res = await client.get("/v1/notifications/push/vapid-public-key", cookies=cookies)
        assert key_res.status_code == 503

        sub_res = await client.post(
            "/v1/notifications/push-subscriptions",
            json=_subscription_payload(f"https://push.example.com/{uuid.uuid4().hex}"),
            cookies=cookies,
        )
        assert sub_res.status_code == 503

        # The delivery job must no-op cleanly, not raise.
        await push_delivery_job()
    finally:
        settings.VAPID_PUBLIC_KEY = "test-public-key"
        settings.VAPID_PRIVATE_KEY = "test-private-key"
