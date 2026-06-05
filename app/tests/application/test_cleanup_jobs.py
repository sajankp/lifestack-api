from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.application.jobs import import_preview_cleanup_job, session_cleanup_job
from app.auth.models import AuthSession, User
from app.auth.repository import AuthSessionRepository, UserRepository
from app.auth.service import AuthService
from app.config import settings
from app.core.database import postgres
from app.imports.models import ImportBatch, ImportPreviewRow
from app.platform.models import Workspace, WorkspaceMembership


async def _seed_user(session, username: str, email: str) -> User:
    user = User(
        username=username,
        email=email,
        hashed_password="hashed_password",
    )
    session.add(user)
    await session.flush()
    return user


@pytest.mark.asyncio
async def test_session_cleanup_job(override_database_url):
    """Verify session_cleanup_job removes expired/revoked sessions but keeps active ones."""
    async with postgres.async_session_maker() as session:
        user = await _seed_user(session, "session_test", "sess@example.com")
        user_id = user.id

        now = datetime.now(UTC)

        # 1. Expired session
        expired = AuthSession(
            user_id=user_id,
            sid="expired-sid",
            expires_at=now - timedelta(seconds=10),
            created_at=now - timedelta(hours=1),
        )
        session.add(expired)

        # 2. Revoked session
        revoked = AuthSession(
            user_id=user_id,
            sid="revoked-sid",
            expires_at=now + timedelta(hours=1),
            revoked_at=now - timedelta(minutes=5),
            created_at=now - timedelta(hours=1),
        )
        session.add(revoked)

        # 3. Active session
        active = AuthSession(
            user_id=user_id,
            sid="active-sid",
            expires_at=now + timedelta(hours=1),
            created_at=now,
        )
        session.add(active)

        await session.commit()

    # Run cleanup job
    await session_cleanup_job()

    async with postgres.async_session_maker() as session:
        sessions = (await session.execute(select(AuthSession))).scalars().all()
        assert len(sessions) == 1
        assert sessions[0].sid == "active-sid"


@pytest.mark.asyncio
async def test_session_limit_eviction(override_database_url):
    """Verify that creating a 6th session evicts the oldest active session."""
    async with postgres.async_session_maker() as session:
        user = await _seed_user(session, "session_limit_test", "sess_limit@example.com")
        user_id = user.id
        await session.commit()

    async with postgres.async_session_maker() as session:
        user_repo = UserRepository(session)
        session_repo = AuthSessionRepository(session)
        service = AuthService(user_repo, session_repo)

        # Create MAX_ACTIVE_SESSIONS_PER_USER sessions
        sids = [f"sid-{i}" for i in range(settings.MAX_ACTIVE_SESSIONS_PER_USER)]
        sessions = []
        for sid in sids:
            # Stagger creation time slightly so touch times/IDs order them
            sess = await service.create_session(user_id, sid, timedelta(hours=1))
            sessions.append(sess)
            # Simulate touching to update last_seen_at
            await service.touch_session(sid, user_id)

        await session.commit()

    # Verify we have 5 active sessions
    async with postgres.async_session_maker() as session:
        user_repo = UserRepository(session)
        session_repo = AuthSessionRepository(session)
        active = await session_repo.get_active_sessions_by_user_id(user_id)
        assert len(active) == 5

    # Create 6th session - should revoke the oldest
    async with postgres.async_session_maker() as session:
        user_repo = UserRepository(session)
        session_repo = AuthSessionRepository(session)
        service = AuthService(user_repo, session_repo)

        await service.create_session(user_id, "sid-6", timedelta(hours=1))
        await session.commit()

    # Verify oldest is now revoked, and active sessions count is still 5
    async with postgres.async_session_maker() as session:
        user_repo = UserRepository(session)
        session_repo = AuthSessionRepository(session)
        active = await session_repo.get_active_sessions_by_user_id(user_id)
        assert len(active) == 5
        active_sids = {s.sid for s in active}
        assert "sid-6" in active_sids
        assert "sid-0" not in active_sids  # the oldest one should be evicted


@pytest.mark.asyncio
async def test_import_preview_cleanup_job(override_database_url):
    """Verify import_preview_cleanup_job purges old preview rows but preserves new ones."""
    async with postgres.async_session_maker() as session:
        user = await _seed_user(session, "import_test", "import@example.com")
        user_id = user.id
        ws = Workspace(id=1, name="Test Workspace", is_active=True)
        session.add(ws)
        await session.flush()
        mem = WorkspaceMembership(workspace_id=1, user_id=user_id, role="owner")
        session.add(mem)
        await session.flush()

        now = datetime.now(UTC)

        # 1. Stale import batch (25 hours ago)
        stale_batch = ImportBatch(
            workspace_id=1,
            user_id=user_id,
            module="spending-transactions",
            status="validated",
            filename="stale.csv",
            file_sha256="stale-sha",
            created_at=now - timedelta(hours=25),
        )
        session.add(stale_batch)
        await session.flush()

        stale_row = ImportPreviewRow(
            import_batch_id=stale_batch.id,
            row_number=2,
            payload_json={"amount": "10.00"},
        )
        session.add(stale_row)

        # 2. Fresh import batch (1 hour ago)
        fresh_batch = ImportBatch(
            workspace_id=1,
            user_id=user_id,
            module="spending-transactions",
            status="validated",
            filename="fresh.csv",
            file_sha256="fresh-sha",
            created_at=now - timedelta(hours=1),
        )
        session.add(fresh_batch)
        await session.flush()

        fresh_row = ImportPreviewRow(
            import_batch_id=fresh_batch.id,
            row_number=2,
            payload_json={"amount": "20.00"},
        )
        session.add(fresh_row)

        await session.commit()

    # Run cleanup job
    await import_preview_cleanup_job()

    async with postgres.async_session_maker() as session:
        previews = (await session.execute(select(ImportPreviewRow))).scalars().all()
        assert len(previews) == 1
        assert previews[0].payload_json["amount"] == "20.00"
