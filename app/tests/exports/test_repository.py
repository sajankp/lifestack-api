import pytest

from app.auth.models import User
from app.core.database import postgres
from app.exports.models import ExportFormat, ExportRecord, ExportStatus
from app.exports.repository import ExportRepository
from app.platform.models import Workspace


async def _seed_workspace_and_user(session, workspace_id: int, user_id: int):
    user = User(
        id=user_id,
        email=f"user{user_id}@example.com",
        username=f"user{user_id}",
        hashed_password="hashed_password",
    )
    session.add(user)
    workspace = Workspace(id=workspace_id, name=f"WS {workspace_id}")
    session.add(workspace)
    await session.commit()


@pytest.mark.asyncio
async def test_export_repository_lifecycle(override_database_url):
    workspace_id = 801
    user_id = 81

    async with postgres.async_session_maker() as session:
        await _seed_workspace_and_user(session, workspace_id, user_id)

    async with postgres.async_session_maker() as session:
        repo = ExportRepository(session)

        # 1. Create record
        record = ExportRecord(
            workspace_id=workspace_id,
            requested_by=user_id,
            format=ExportFormat.json,
            scope={"modules": ["todo"]},
            status=ExportStatus.pending,
        )
        created = await repo.create(record)
        await session.commit()

        assert created.id is not None
        assert created.public_id is not None
        assert created.status == ExportStatus.pending
        public_id = created.public_id

    # 2. Get by public ID
    async with postgres.async_session_maker() as session:
        repo = ExportRepository(session)
        fetched = await repo.get_by_public_id(workspace_id, public_id)
        assert fetched is not None
        assert fetched.id == created.id

        # Should return None if workspace ID mismatches
        assert await repo.get_by_public_id(999, public_id) is None

    # 3. Get pending for workspace
    async with postgres.async_session_maker() as session:
        repo = ExportRepository(session)
        pending = await repo.get_pending_for_workspace(workspace_id)
        assert pending is not None
        assert pending.id == created.id

        # No pending in other workspace
        assert await repo.get_pending_for_workspace(999) is None

    # 4. Save / Update record
    async with postgres.async_session_maker() as session:
        repo = ExportRepository(session)
        fetched = await repo.get_by_public_id(workspace_id, public_id)
        fetched.status = ExportStatus.ready
        fetched.storage_key = "db://test"
        updated = await repo.save(fetched)
        await session.commit()

        assert updated.status == ExportStatus.ready
        assert updated.storage_key == "db://test"

    # Verify no longer pending
    async with postgres.async_session_maker() as session:
        repo = ExportRepository(session)
        assert await repo.get_pending_for_workspace(workspace_id) is None

    # 5. List workspace exports
    async with postgres.async_session_maker() as session:
        repo = ExportRepository(session)

        # Add another record
        record2 = ExportRecord(
            workspace_id=workspace_id,
            requested_by=user_id,
            format=ExportFormat.csv,
            scope={"modules": ["spending"]},
            status=ExportStatus.failed,
        )
        await repo.create(record2)
        await session.commit()

        exports = await repo.list_workspace_exports(workspace_id, limit=10)
        assert len(exports) == 2
        # Ordered by created_at desc, so record2 should be first
        assert exports[0].format == ExportFormat.csv
        assert exports[1].format == ExportFormat.json
