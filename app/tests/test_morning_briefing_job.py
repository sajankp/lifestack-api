"""Integration tests for morning_briefing_job (spec-067)."""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.application.jobs import MORNING_BRIEFING_LOCK_KEY, morning_briefing_job
from app.auth.models import User
from app.core.database import postgres
from app.notifications.models import Notification
from app.platform.models import Workspace, WorkspaceMembership
from app.todo.models import Todo


async def _seed_workspace(session, workspace_id: int, user_id: int, email: str, username: str):
    session.add(
        User(id=user_id, email=email, username=username, hashed_password="hashed_password_here")
    )
    session.add(Workspace(id=workspace_id, name=f"ws-{workspace_id}"))
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace_id, user_id=user_id, role="owner"))
    await session.flush()


@pytest.mark.asyncio
async def test_morning_briefing_job_writes_notification_when_not_all_clear(
    override_database_url,
):
    async with postgres.async_session_maker() as session, session.begin():
        await _seed_workspace(session, 601, 1, "briefing_job1@example.com", "briefing_job1")
        session.add(
            Todo(
                workspace_id=601,
                user_id=1,
                title="Overdue job todo",
                priority="high",
                due_date=datetime.now(UTC) - timedelta(days=1),
                completed=False,
            )
        )

    await morning_briefing_job(workspace_id=601)

    async with postgres.async_session_maker() as session:
        result = await session.execute(
            select(Notification).where(
                Notification.workspace_id == 601, Notification.category == "briefing"
            )
        )
        notifications = result.scalars().all()

    assert len(notifications) == 1
    assert notifications[0].severity == "critical"
    assert notifications[0].title == "Morning briefing"
    assert "Overdue job todo" in (notifications[0].body or "")


@pytest.mark.asyncio
async def test_morning_briefing_job_skips_all_clear_workspace(override_database_url):
    async with postgres.async_session_maker() as session, session.begin():
        await _seed_workspace(session, 602, 2, "briefing_job2@example.com", "briefing_job2")

    await morning_briefing_job(workspace_id=602)

    async with postgres.async_session_maker() as session:
        result = await session.execute(
            select(Notification).where(
                Notification.workspace_id == 602, Notification.category == "briefing"
            )
        )
        assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_morning_briefing_job_skips_when_lock_held(
    override_database_url, test_database_engine
):
    """A second concurrent instance must skip, not double-write the
    briefing notification (same regression class as investment_closing_prices_job)."""
    async with postgres.async_session_maker() as session, session.begin():
        await _seed_workspace(session, 603, 3, "briefing_job3@example.com", "briefing_job3")
        session.add(
            Todo(
                workspace_id=603,
                user_id=3,
                title="Overdue lock test todo",
                priority="high",
                due_date=datetime.now(UTC) - timedelta(days=1),
                completed=False,
            )
        )

    other_connection = await test_database_engine.connect()
    try:
        lock_res = await other_connection.execute(
            sa.select(sa.func.pg_try_advisory_lock(MORNING_BRIEFING_LOCK_KEY))
        )
        assert lock_res.scalar() is True, (
            "setup: failed to acquire the lock on the other connection"
        )

        await morning_briefing_job(workspace_id=603)

        async with postgres.async_session_maker() as session:
            result = await session.execute(
                select(Notification).where(
                    Notification.workspace_id == 603, Notification.category == "briefing"
                )
            )
            assert result.scalars().all() == [], "job should have skipped while the lock was held"
    finally:
        await other_connection.execute(
            sa.select(sa.func.pg_advisory_unlock(MORNING_BRIEFING_LOCK_KEY))
        )
        await other_connection.commit()
        await other_connection.close()
