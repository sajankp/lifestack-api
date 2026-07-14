import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.application.jobs import email_delivery_job
from app.auth.repository import UserRepository
from app.config import settings
from app.core.database import postgres
from app.notifications.email import EmailResult, send_email
from app.notifications.models import NotificationDelivery
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService
from app.platform.repository import WorkspaceRepository
from app.tests.integration.test_spending import _register_and_login


@pytest.fixture(autouse=True)
def _email_configured():
    original = (settings.RESEND_API_KEY, settings.EMAIL_FROM_ADDRESS, settings.EMAIL_ENABLED)
    settings.RESEND_API_KEY = "test-resend-key"
    settings.EMAIL_FROM_ADDRESS = "notifications@example.com"
    settings.EMAIL_ENABLED = True
    yield
    settings.RESEND_API_KEY, settings.EMAIL_FROM_ADDRESS, settings.EMAIL_ENABLED = original


async def _workspace_and_user_id(creds: dict) -> tuple[int, int]:
    async with postgres.async_session_maker() as session:
        user = await UserRepository(session).get_by_username(creds["username"])
        assert user is not None
        workspaces = await WorkspaceRepository(session).list_user_workspaces(user.id)
        return workspaces[0].id, user.id


@pytest.mark.asyncio
async def test_send_email_skipped_when_disabled():
    settings.EMAIL_ENABLED = False
    result = await send_email("user@example.com", "Subject", "<p>Body</p>")
    assert result == EmailResult(success=False, skipped=True)


@pytest.mark.asyncio
async def test_send_email_posts_to_resend():
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    result = await send_email("user@example.com", "Subject", "<p>Body</p>", client=mock_client)

    assert result.success is True
    mock_client.post.assert_awaited_once()
    call_kwargs = mock_client.post.call_args.kwargs
    assert call_kwargs["json"]["to"] == ["user@example.com"]
    assert call_kwargs["json"]["from"] == settings.EMAIL_FROM_ADDRESS
    assert call_kwargs["headers"]["Authorization"] == f"Bearer {settings.RESEND_API_KEY}"


@pytest.mark.asyncio
async def test_send_email_failure_never_raises():
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("boom"))

    result = await send_email("user@example.com", "Subject", "<p>Body</p>", client=mock_client)

    assert result.success is False
    assert result.skipped is False
    assert "boom" in (result.error_detail or "")


@pytest.mark.asyncio
async def test_notify_creates_pending_email_delivery_when_preference_on(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    cookies = creds["cookies"]
    workspace_id, user_id = await _workspace_and_user_id(creds)

    await client.patch(
        "/v1/notifications/preferences/todo_reminder",
        json={"channel_email": True},
        cookies=cookies,
    )

    async with postgres.async_session_maker() as session:
        service = NotificationService(NotificationRepository(session))
        await service.notify(
            workspace_id=workspace_id,
            user_id=user_id,
            category="todo_reminder",
            severity="info",
            title="Deliver me by email",
        )
        await session.commit()

    async with postgres.async_session_maker() as session:
        deliveries = (await session.execute(select(NotificationDelivery))).scalars().all()
        assert sorted(d.channel for d in deliveries) == ["email", "in_app"]
        email_delivery = next(d for d in deliveries if d.channel == "email")
        assert email_delivery.status == "pending"


@pytest.mark.asyncio
async def test_notify_skips_email_delivery_when_preference_off(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    workspace_id, user_id = await _workspace_and_user_id(creds)

    async with postgres.async_session_maker() as session:
        service = NotificationService(NotificationRepository(session))
        await service.notify(
            workspace_id=workspace_id,
            user_id=user_id,
            category="todo_reminder",
            severity="info",
            title="No email please",
        )
        await session.commit()

    async with postgres.async_session_maker() as session:
        deliveries = (await session.execute(select(NotificationDelivery))).scalars().all()
        assert [d.channel for d in deliveries] == ["in_app"]


@pytest.mark.asyncio
async def test_email_delivery_job_drains_pending_and_is_idempotent(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    cookies = creds["cookies"]
    workspace_id, user_id = await _workspace_and_user_id(creds)

    await client.patch(
        "/v1/notifications/preferences/todo_reminder",
        json={"channel_email": True},
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
        "app.application.workflows.send_email",
        new=AsyncMock(return_value=EmailResult(success=True)),
    ) as mock_send:
        await email_delivery_job()
    mock_send.assert_awaited_once()

    async with postgres.async_session_maker() as session:
        deliveries = (
            (
                await session.execute(
                    select(NotificationDelivery).where(NotificationDelivery.channel == "email")
                )
            )
            .scalars()
            .all()
        )
        assert [d.status for d in deliveries] == ["sent"]

    with patch(
        "app.application.workflows.send_email",
        new=AsyncMock(return_value=EmailResult(success=True)),
    ) as mock_send_again:
        await email_delivery_job()
    mock_send_again.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_delivery_job_inert_when_email_disabled(client: AsyncClient):
    settings.EMAIL_ENABLED = False
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    cookies = creds["cookies"]
    workspace_id, user_id = await _workspace_and_user_id(creds)

    await client.patch(
        "/v1/notifications/preferences/todo_reminder",
        json={"channel_email": True},
        cookies=cookies,
    )

    async with postgres.async_session_maker() as session:
        service = NotificationService(NotificationRepository(session))
        await service.notify(
            workspace_id=workspace_id,
            user_id=user_id,
            category="todo_reminder",
            severity="info",
            title="Should stay pending",
        )
        await session.commit()

    with patch("app.application.workflows.send_email", new=AsyncMock()) as mock_send:
        await email_delivery_job()
    mock_send.assert_not_awaited()

    async with postgres.async_session_maker() as session:
        deliveries = (
            (
                await session.execute(
                    select(NotificationDelivery).where(NotificationDelivery.channel == "email")
                )
            )
            .scalars()
            .all()
        )
        assert [d.status for d in deliveries] == ["pending"]
