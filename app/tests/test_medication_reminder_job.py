"""Integration tests for medication_reminder_job (spec-069)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.application.jobs import medication_reminder_job
from app.auth.models import User
from app.core.database import postgres
from app.health.models import Medication
from app.notifications.models import Notification
from app.platform.models import Workspace, WorkspaceMembership


async def _seed_workspace(session, workspace_id: int, user_id: int, email: str, username: str):
    session.add(
        User(id=user_id, email=email, username=username, hashed_password="hashed_password_here")
    )
    session.add(Workspace(id=workspace_id, name=f"ws-{workspace_id}"))
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace_id, user_id=user_id, role="owner"))
    await session.flush()


@pytest.mark.asyncio
async def test_medication_reminder_job_creates_one_notification_per_due_slot(
    override_database_url,
):
    now = datetime.now(UTC)
    # A minute ahead — HEALTH_REMINDER_INTERVAL_MINUTES-sized window starting
    # at `now` — avoids the boundary case where truncating "HH:MM" to
    # whole-minute precision puts the slot fractionally before `now` itself.
    dose_time = (now + timedelta(minutes=1)).strftime("%H:%M")
    async with postgres.async_session_maker() as session, session.begin():
        await _seed_workspace(session, 701, 1, "medjob1@example.com", "medjob1")
        session.add(
            Medication(
                workspace_id=701,
                user_id=1,
                name="Metformin",
                dose_text="500 mg",
                frequency="daily",
                interval=1,
                anchor_date=date(2026, 1, 1),
                timezone="UTC",
                times=[dose_time],
                is_active=True,
                reminders_enabled=True,
            )
        )

    await medication_reminder_job(workspace_id=701)

    async with postgres.async_session_maker() as session:
        result = await session.execute(
            select(Notification).where(
                Notification.workspace_id == 701,
                Notification.category == "medication_reminder",
            )
        )
        notifications = result.scalars().all()

    assert len(notifications) == 1
    assert notifications[0].title == "Metformin"

    # Re-running the job in the same window must not create a duplicate
    # (idempotent via Medication.last_reminded_slot).
    await medication_reminder_job(workspace_id=701)

    async with postgres.async_session_maker() as session:
        result = await session.execute(
            select(Notification).where(
                Notification.workspace_id == 701,
                Notification.category == "medication_reminder",
            )
        )
        notifications_after_rerun = result.scalars().all()

    assert len(notifications_after_rerun) == 1


@pytest.mark.asyncio
async def test_medication_reminder_job_skips_disabled_and_paused_medications(
    override_database_url,
):
    now = datetime.now(UTC)
    dose_time = now.strftime("%H:%M")
    async with postgres.async_session_maker() as session, session.begin():
        await _seed_workspace(session, 702, 1, "medjob2@example.com", "medjob2")
        session.add(
            Medication(
                workspace_id=702,
                user_id=1,
                name="Paused Med",
                frequency="daily",
                interval=1,
                anchor_date=date(2026, 1, 1),
                timezone="UTC",
                times=[dose_time],
                is_active=False,
                reminders_enabled=True,
            )
        )
        session.add(
            Medication(
                workspace_id=702,
                user_id=1,
                name="Reminders Off Med",
                frequency="daily",
                interval=1,
                anchor_date=date(2026, 1, 1),
                timezone="UTC",
                times=[dose_time],
                is_active=True,
                reminders_enabled=False,
            )
        )

    await medication_reminder_job(workspace_id=702)

    async with postgres.async_session_maker() as session:
        result = await session.execute(
            select(Notification).where(
                Notification.workspace_id == 702,
                Notification.category == "medication_reminder",
            )
        )
        notifications = result.scalars().all()

    assert len(notifications) == 0


@pytest.mark.asyncio
async def test_medication_reminder_job_skips_course_ended_medication(override_database_url):
    now = datetime.now(UTC)
    dose_time = now.strftime("%H:%M")
    async with postgres.async_session_maker() as session, session.begin():
        await _seed_workspace(session, 703, 1, "medjob3@example.com", "medjob3")
        session.add(
            Medication(
                workspace_id=703,
                user_id=1,
                name="Ended Course",
                frequency="daily",
                interval=1,
                anchor_date=date(2020, 1, 1),
                end_date=(now - timedelta(days=1)).date(),
                timezone="UTC",
                times=[dose_time],
                is_active=True,
                reminders_enabled=True,
            )
        )

    await medication_reminder_job(workspace_id=703)

    async with postgres.async_session_maker() as session:
        result = await session.execute(
            select(Notification).where(
                Notification.workspace_id == 703,
                Notification.category == "medication_reminder",
            )
        )
        notifications = result.scalars().all()

    assert len(notifications) == 0
