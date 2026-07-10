"""Tests for the `python -m app.cli.send_push` CLI (spec-052).

Mirrors the end-to-end send path verified by hand: resolve devices → send via
the same `send_web_push` used by `push_delivery_job` → record per-device
outcome, deactivate gone endpoints, optionally persist an in-app campaign
notification.
"""

import base64
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest
import pywebpush
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import AsyncClient
from py_vapid import Vapid
from sqlalchemy import select

from app.cli import send_push
from app.cli.send_push import main, resolve_targets, send_to_devices
from app.config import settings
from app.core.database import postgres
from app.notifications.models import Notification, NotificationDelivery, PushSubscription
from app.notifications.push import PushResult
from app.tests.integration.test_spending import _register_and_login
from app.tests.notifications.test_push_notifications import _workspace_and_user_id


@pytest.fixture(autouse=True)
def _vapid_configured():
    original = (settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY, settings.VAPID_SUBJECT)
    settings.VAPID_PUBLIC_KEY = "test-public-key"
    settings.VAPID_PRIVATE_KEY = "test-private-key"
    settings.VAPID_SUBJECT = "mailto:test@example.com"
    yield
    settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY, settings.VAPID_SUBJECT = original


async def _insert_subscription(
    workspace_id: int, user_id: int, *, label: str = "Test device", is_active: bool = True
) -> int:
    async with postgres.async_session_maker() as session:
        sub = PushSubscription(
            workspace_id=workspace_id,
            user_id=user_id,
            endpoint=f"https://push.example.com/{uuid.uuid4().hex}",
            p256dh="test-p256dh-key",
            auth="test-auth-secret",
            device_label=label,
            is_active=is_active,
        )
        session.add(sub)
        await session.commit()
        return sub.id


async def _sub_by_id(sub_id: int) -> PushSubscription:
    async with postgres.async_session_maker() as session:
        return (
            await session.execute(select(PushSubscription).where(PushSubscription.id == sub_id))
        ).scalar_one()


@pytest.mark.asyncio
async def test_send_to_devices_success_records_last_success(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    workspace_id, user_id = await _workspace_and_user_id(creds)
    sub_id = await _insert_subscription(workspace_id, user_id)

    with patch.object(send_push, "send_web_push", MagicMock(return_value=PushResult(success=True))):
        async with postgres.async_session_maker() as session:
            subs = await resolve_targets(session, user_id=user_id)
            summary = await send_to_devices(session, subs, title="Hi", body="there")
            await session.commit()

    assert summary == {"sent": 1, "failed": 0, "deactivated": 0, "persisted": 0}
    sub = await _sub_by_id(sub_id)
    assert sub.last_success_at is not None
    assert sub.is_active is True


@pytest.mark.asyncio
async def test_send_to_devices_gone_deactivates_subscription(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    workspace_id, user_id = await _workspace_and_user_id(creds)
    sub_id = await _insert_subscription(workspace_id, user_id)

    gone = MagicMock(return_value=PushResult(success=False, gone=True, error_detail="410 Gone"))
    with patch.object(send_push, "send_web_push", gone):
        async with postgres.async_session_maker() as session:
            subs = await resolve_targets(session, user_id=user_id)
            summary = await send_to_devices(session, subs, title="Hi", body="there")
            await session.commit()

    assert summary == {"sent": 0, "failed": 1, "deactivated": 1, "persisted": 0}
    sub = await _sub_by_id(sub_id)
    assert sub.is_active is False
    assert sub.last_failure_at is not None


@pytest.mark.asyncio
async def test_send_to_devices_gone_kept_when_deactivate_disabled(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    workspace_id, user_id = await _workspace_and_user_id(creds)
    sub_id = await _insert_subscription(workspace_id, user_id)

    gone = MagicMock(return_value=PushResult(success=False, gone=True, error_detail="410 Gone"))
    with patch.object(send_push, "send_web_push", gone):
        async with postgres.async_session_maker() as session:
            subs = await resolve_targets(session, user_id=user_id)
            summary = await send_to_devices(
                session, subs, title="Hi", body="", deactivate_gone=False
            )
            await session.commit()

    assert summary["deactivated"] == 0
    sub = await _sub_by_id(sub_id)
    assert sub.is_active is True


@pytest.mark.asyncio
async def test_send_to_devices_persist_in_app_creates_campaign_notification(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    workspace_id, user_id = await _workspace_and_user_id(creds)
    await _insert_subscription(workspace_id, user_id)

    with patch.object(send_push, "send_web_push", MagicMock(return_value=PushResult(success=True))):
        async with postgres.async_session_maker() as session:
            subs = await resolve_targets(session, user_id=user_id)
            summary = await send_to_devices(
                session, subs, title="New feature", body="Try it", persist_in_app=True
            )
            await session.commit()

    assert summary["persisted"] == 1
    async with postgres.async_session_maker() as session:
        notif = (
            await session.execute(
                select(Notification).where(
                    Notification.workspace_id == workspace_id,
                    Notification.category == "campaign",
                )
            )
        ).scalar_one()
        assert notif.title == "New feature"
        assert notif.severity == "info"
        # create_notification also records the in-app delivery row.
        delivery = (
            await session.execute(
                select(NotificationDelivery).where(NotificationDelivery.notification_id == notif.id)
            )
        ).scalar_one()
        assert delivery.channel == "in_app"


@pytest.mark.asyncio
async def test_resolve_targets_excludes_inactive_by_default(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    workspace_id, user_id = await _workspace_and_user_id(creds)
    active_id = await _insert_subscription(workspace_id, user_id, label="active")
    await _insert_subscription(workspace_id, user_id, label="dead", is_active=False)

    async with postgres.async_session_maker() as session:
        default = await resolve_targets(session, user_id=user_id)
        assert [s.id for s in default] == [active_id]

        with_inactive = await resolve_targets(session, user_id=user_id, include_inactive=True)
        assert len(with_inactive) == 2


@pytest.mark.asyncio
async def test_resolve_targets_label_substring_filter(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    workspace_id, user_id = await _workspace_and_user_id(creds)
    pixel_id = await _insert_subscription(workspace_id, user_id, label="Pixel 9")
    await _insert_subscription(workspace_id, user_id, label="MacBook")

    async with postgres.async_session_maker() as session:
        matched = await resolve_targets(session, user_id=user_id, label="pixel")
        assert [s.id for s in matched] == [pixel_id]


@pytest.mark.asyncio
async def test_main_send_exits_nonzero_on_failure(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    workspace_id, user_id = await _workspace_and_user_id(creds)
    sub_id = await _insert_subscription(workspace_id, user_id)

    gone = MagicMock(return_value=PushResult(success=False, gone=True, error_detail="410 Gone"))
    argv = ["send_push", "--title", "Hi", "--user-id", str(user_id), "--yes"]
    with (
        patch.object(send_push, "send_web_push", gone),
        patch.object(sys, "argv", argv),
        pytest.raises(SystemExit) as exc_info,
    ):
        await main()
    assert exc_info.value.code == 1
    # The failure path still committed the deactivation before exiting.
    sub = await _sub_by_id(sub_id)
    assert sub.is_active is False


@pytest.mark.asyncio
async def test_main_dry_run_sends_nothing(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    workspace_id, user_id = await _workspace_and_user_id(creds)
    sub_id = await _insert_subscription(workspace_id, user_id)

    send = MagicMock(return_value=PushResult(success=True))
    argv = ["send_push", "--title", "Hi", "--user-id", str(user_id), "--dry-run"]
    with patch.object(send_push, "send_web_push", send), patch.object(sys, "argv", argv):
        await main()
    send.assert_not_called()
    sub = await _sub_by_id(sub_id)
    assert sub.last_success_at is None


@pytest.mark.asyncio
async def test_main_aborts_when_confirmation_declined(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    workspace_id, user_id = await _workspace_and_user_id(creds)
    await _insert_subscription(workspace_id, user_id)

    send = MagicMock(return_value=PushResult(success=True))
    argv = ["send_push", "--title", "Hi", "--user-id", str(user_id)]  # no --yes
    with (
        patch.object(send_push, "send_web_push", send),
        patch.object(sys, "argv", argv),
        patch("builtins.input", return_value="n"),
    ):
        await main()
    send.assert_not_called()


@pytest.mark.asyncio
async def test_main_requires_title_unless_list():
    with patch.object(sys, "argv", ["send_push"]), pytest.raises(SystemExit) as exc_info:
        await main()
    assert exc_info.value.code == 2


@pytest.mark.asyncio
async def test_main_errors_when_vapid_unconfigured():
    settings.VAPID_PRIVATE_KEY = None
    with (
        patch.object(sys, "argv", ["send_push", "--title", "Hi", "--yes"]),
        pytest.raises(SystemExit) as exc_info,
    ):
        await main()
    assert exc_info.value.code == 2


@pytest.mark.asyncio
async def test_main_list_does_not_require_vapid(client: AsyncClient, capsys):
    settings.VAPID_PRIVATE_KEY = None
    with patch.object(sys, "argv", ["send_push", "--list"]):
        await main()
    out = capsys.readouterr().out
    assert "Registered devices" in out


# --- e2e-style: run the REAL send path (real VAPID signing + real payload
# encryption to a real client key), faking only the network socket. This is the
# by-hand check — insert a real subscription, send, observe the outcome — turned
# into a repeatable test that exercises everything short of the Google hop.


def _real_vapid_keys() -> tuple[str, str]:
    v = Vapid()
    v.generate_keys()
    pub = (
        base64
        .urlsafe_b64encode(
            v.public_key.public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
        )
        .rstrip(b"=")
        .decode()
    )
    priv = (
        base64
        .urlsafe_b64encode(v.private_key.private_numbers().private_value.to_bytes(32, "big"))
        .rstrip(b"=")
        .decode()
    )
    return pub, priv


def _real_client_keys() -> tuple[str, str]:
    """A real EC P-256 public point (p256dh) + 16-byte auth secret — valid inputs
    for pywebpush's aes128gcm payload encryption."""
    key = ec.generate_private_key(ec.SECP256R1())
    p256dh = (
        base64
        .urlsafe_b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
        )
        .rstrip(b"=")
        .decode()
    )
    auth = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()
    return p256dh, auth


class _FakeResponse:
    def __init__(self, status_code: int, reason: str = "", text: str = ""):
        self.status_code = status_code
        self.reason = reason
        self.text = text
        self.headers: dict = {}


async def _insert_real_key_subscription(workspace_id: int, user_id: int) -> int:
    p256dh, auth = _real_client_keys()
    async with postgres.async_session_maker() as session:
        sub = PushSubscription(
            workspace_id=workspace_id,
            user_id=user_id,
            endpoint=f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4().hex}",
            p256dh=p256dh,
            auth=auth,
            device_label="Real-key device",
        )
        session.add(sub)
        await session.commit()
        return sub.id


@pytest.fixture
def _real_vapid_env():
    original = (settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY, settings.VAPID_SUBJECT)
    pub, priv = _real_vapid_keys()
    settings.VAPID_PUBLIC_KEY = pub
    settings.VAPID_PRIVATE_KEY = priv
    settings.VAPID_SUBJECT = "mailto:test@example.com"
    yield
    settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY, settings.VAPID_SUBJECT = original


@pytest.mark.asyncio
async def test_e2e_real_encryption_success(client: AsyncClient, _real_vapid_env):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    workspace_id, user_id = await _workspace_and_user_id(creds)
    sub_id = await _insert_real_key_subscription(workspace_id, user_id)

    captured = {}

    def fake_post(endpoint, data=None, headers=None, timeout=None, **kwargs):
        # Real VAPID signing + payload encryption already ran to get here.
        captured["endpoint"] = endpoint
        captured["headers"] = headers or {}
        captured["body_len"] = len(data or b"")
        return _FakeResponse(201, "Created")

    with patch.object(pywebpush.requests, "post", side_effect=fake_post):
        async with postgres.async_session_maker() as session:
            subs = await resolve_targets(session, user_id=user_id)
            summary = await send_to_devices(session, subs, title="Real", body="payload")
            await session.commit()

    assert summary == {"sent": 1, "failed": 0, "deactivated": 0, "persisted": 0}
    # Proof the real crypto path executed: an encrypted body was produced and a
    # VAPID Authorization header was signed.
    assert captured["body_len"] > 0
    auth_header = captured["headers"].get("Authorization") or captured["headers"].get(
        "authorization"
    )
    assert auth_header is not None and "vapid" in auth_header.lower()
    sub = await _sub_by_id(sub_id)
    assert sub.last_success_at is not None


@pytest.mark.asyncio
async def test_e2e_real_encryption_gone_deactivates(client: AsyncClient, _real_vapid_env):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    workspace_id, user_id = await _workspace_and_user_id(creds)
    sub_id = await _insert_real_key_subscription(workspace_id, user_id)

    def fake_post(*args, **kwargs):
        return _FakeResponse(410, "Gone", "push subscription has unsubscribed or expired.")

    with patch.object(pywebpush.requests, "post", side_effect=fake_post):
        async with postgres.async_session_maker() as session:
            subs = await resolve_targets(session, user_id=user_id)
            summary = await send_to_devices(session, subs, title="Real", body="payload")
            await session.commit()

    assert summary == {"sent": 0, "failed": 1, "deactivated": 1, "persisted": 0}
    sub = await _sub_by_id(sub_id)
    assert sub.is_active is False
    assert sub.last_failure_at is not None
