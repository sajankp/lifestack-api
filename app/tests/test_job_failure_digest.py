"""spec-088 Layer C tests (testing plan items 9-14): daily digest + weekly
heartbeat + retention purge."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.application.jobs import (
    export_cleanup_job,
    job_failure_digest_job,
    job_health_heartbeat_job,
)
from app.auth.models import User
from app.config import settings
from app.core.database import postgres
from app.core.job_failures import JobFailure
from app.notifications.email import EmailResult
from app.notifications.models import Notification
from app.platform.models import Workspace, WorkspaceMembership


@pytest.fixture(autouse=True)
async def seed_owner_workspace(override_database_url):
    async with postgres.async_session_maker() as session:
        session.add(
            User(
                id=1,
                email="owner@example.com",
                username="owner",
                hashed_password="hashed_password_here",
            )
        )
        session.add(Workspace(id=901, name="Owner Workspace"))
        await session.flush()
        session.add(WorkspaceMembership(workspace_id=901, user_id=1, role="owner"))
        await session.commit()


async def _seed_failure(
    job_name: str,
    workspace_id: int | None = None,
    notified: bool = False,
    resolved: bool = False,
    created_at: datetime | None = None,
) -> None:
    async with postgres.async_session_maker() as session, session.begin():
        session.add(
            JobFailure(
                job_name=job_name,
                workspace_id=workspace_id,
                error_type="RuntimeError",
                error_message="boom",
                attempts=1,
                first_failed_at=datetime.now(UTC),
                created_at=created_at or datetime.now(UTC),
                notified_at=datetime.now(UTC) if notified else None,
                resolved_at=datetime.now(UTC) if resolved else None,
            )
        )


@pytest.mark.asyncio
async def test_digest_sends_one_email_and_one_notification_and_stamps_notified(
    override_database_url, monkeypatch
):
    monkeypatch.setattr(settings, "OWNER_ALERT_EMAIL", "owner@example.com")
    await _seed_failure("fx_rate_ingestion_job")
    await _seed_failure("investment_closing_prices_job", workspace_id=901)

    email_mock = AsyncMock(return_value=EmailResult(success=True))
    with patch("app.application.jobs.send_email", new=email_mock):
        await job_failure_digest_job()

    assert email_mock.await_count == 1

    async with postgres.async_session_maker() as session:
        rows = (await session.execute(select(JobFailure))).scalars().all()
        notifications = (await session.execute(select(Notification))).scalars().all()

    assert all(r.notified_at is not None for r in rows)
    assert len(notifications) == 1
    assert notifications[0].workspace_id == 901


@pytest.mark.asyncio
async def test_digest_with_no_unnotified_rows_sends_nothing(override_database_url, monkeypatch):
    monkeypatch.setattr(settings, "OWNER_ALERT_EMAIL", "owner@example.com")
    await _seed_failure("fx_rate_ingestion_job", notified=True)

    email_mock = AsyncMock(return_value=EmailResult(success=True))
    with patch("app.application.jobs.send_email", new=email_mock):
        await job_failure_digest_job()

    email_mock.assert_not_called()
    async with postgres.async_session_maker() as session:
        notifications = (await session.execute(select(Notification))).scalars().all()
    assert notifications == []


@pytest.mark.asyncio
async def test_digest_without_owner_alert_email_skips_email_and_in_app_notification(
    override_database_url, monkeypatch
):
    """OWNER_ALERT_EMAIL is how the owner's user account is resolved for the
    in-app notification (matched against User.email) -- without it there's no
    identity to notify, so both channels are skipped. The ledger rows are
    still stamped notified_at so a later run doesn't re-report them once
    OWNER_ALERT_EMAIL is configured."""
    monkeypatch.setattr(settings, "OWNER_ALERT_EMAIL", None)
    await _seed_failure("fx_rate_ingestion_job")

    email_mock = AsyncMock(return_value=EmailResult(success=True))
    with patch("app.application.jobs.send_email", new=email_mock):
        await job_failure_digest_job()

    email_mock.assert_not_called()
    async with postgres.async_session_maker() as session:
        notifications = (await session.execute(select(Notification))).scalars().all()
        rows = (await session.execute(select(JobFailure))).scalars().all()
    assert notifications == []
    assert all(r.notified_at is not None for r in rows)


@pytest.mark.asyncio
async def test_digest_owner_email_not_matching_any_user_skips_in_app_notification(
    override_database_url, monkeypatch
):
    """OWNER_ALERT_EMAIL set but not matching any User row (e.g. misconfigured)
    must not create an in-app notification for the wrong/no user -- email
    still sends since Resend doesn't need a local User match."""
    monkeypatch.setattr(settings, "OWNER_ALERT_EMAIL", "nobody@example.com")
    await _seed_failure("fx_rate_ingestion_job")

    email_mock = AsyncMock(return_value=EmailResult(success=True))
    with patch("app.application.jobs.send_email", new=email_mock):
        await job_failure_digest_job()

    email_mock.assert_awaited_once()
    async with postgres.async_session_maker() as session:
        notifications = (await session.execute(select(Notification))).scalars().all()
    assert notifications == []


@pytest.mark.asyncio
async def test_digest_disabled_is_a_no_op(override_database_url, monkeypatch):
    monkeypatch.setattr(settings, "JOB_FAILURE_DIGEST_ENABLED", False)
    monkeypatch.setattr(settings, "OWNER_ALERT_EMAIL", "owner@example.com")
    await _seed_failure("fx_rate_ingestion_job")

    email_mock = AsyncMock(return_value=EmailResult(success=True))
    with patch("app.application.jobs.send_email", new=email_mock):
        await job_failure_digest_job()

    email_mock.assert_not_called()
    async with postgres.async_session_maker() as session:
        rows = (await session.execute(select(JobFailure))).scalars().all()
    assert all(r.notified_at is None for r in rows)


@pytest.mark.asyncio
async def test_heartbeat_sends_even_with_zero_failures(override_database_url, monkeypatch):
    monkeypatch.setattr(settings, "OWNER_ALERT_EMAIL", "owner@example.com")

    email_mock = AsyncMock(return_value=EmailResult(success=True))
    with patch("app.application.jobs.send_email", new=email_mock):
        await job_health_heartbeat_job()

    email_mock.assert_awaited_once()
    _, kwargs = email_mock.await_args
    assert "No job failures" in kwargs["html"]


@pytest.mark.asyncio
async def test_heartbeat_respects_disabled_flag(override_database_url, monkeypatch):
    monkeypatch.setattr(settings, "OWNER_ALERT_EMAIL", "owner@example.com")
    monkeypatch.setattr(settings, "JOB_HEALTH_HEARTBEAT_ENABLED", False)

    email_mock = AsyncMock(return_value=EmailResult(success=True))
    with patch("app.application.jobs.send_email", new=email_mock):
        await job_health_heartbeat_job()

    email_mock.assert_not_called()


@pytest.mark.asyncio
async def test_heartbeat_summarizes_last_seven_days(override_database_url, monkeypatch):
    monkeypatch.setattr(settings, "OWNER_ALERT_EMAIL", "owner@example.com")
    now = datetime.now(UTC)
    await _seed_failure("fx_rate_ingestion_job", resolved=True, created_at=now - timedelta(days=1))
    await _seed_failure("fx_rate_ingestion_job", created_at=now - timedelta(days=2))
    # Outside the 7-day window -- must not be counted.
    await _seed_failure("fx_rate_ingestion_job", created_at=now - timedelta(days=10))

    email_mock = AsyncMock(return_value=EmailResult(success=True))
    with patch("app.application.jobs.send_email", new=email_mock):
        await job_health_heartbeat_job()

    _, kwargs = email_mock.await_args
    assert "2 job failure(s)" in kwargs["html"]
    assert "1 auto-recovered" in kwargs["html"]
    assert "1 still open" in kwargs["html"]


@pytest.mark.asyncio
async def test_export_cleanup_job_purges_resolved_job_failures_past_retention(
    override_database_url,
):
    now = datetime.now(UTC)
    async with postgres.async_session_maker() as session, session.begin():
        session.add(
            JobFailure(
                job_name="fx_rate_ingestion_job",
                error_type="RuntimeError",
                error_message="old resolved",
                attempts=1,
                first_failed_at=now - timedelta(days=100),
                resolved_at=now - timedelta(days=95),
            )
        )
        session.add(
            JobFailure(
                job_name="fx_rate_ingestion_job",
                error_type="RuntimeError",
                error_message="recently resolved",
                attempts=1,
                first_failed_at=now - timedelta(days=5),
                resolved_at=now - timedelta(days=1),
            )
        )
        session.add(
            JobFailure(
                job_name="fx_rate_ingestion_job",
                error_type="RuntimeError",
                error_message="still open",
                attempts=1,
                first_failed_at=now - timedelta(days=200),
            )
        )

    await export_cleanup_job()

    async with postgres.async_session_maker() as session:
        remaining = (
            (await session.execute(select(JobFailure.error_message).order_by(JobFailure.id)))
            .scalars()
            .all()
        )

    assert remaining == ["recently resolved", "still open"]
