"""McpGrantRepository.touch must not write on every authenticated MCP request.

LifestackTokenVerifier.load_access_token runs on every MCP call and touched the grant each time:
~1,500 single-row UPDATE transactions in two days for one user, on the auth path, on a
throttled disk. ``last_used_at`` is display metadata, so it only needs minute-level accuracy.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.mcp.models import McpGrant
from app.mcp.repository import GRANT_TOUCH_MIN_INTERVAL, McpGrantRepository


def _grant(last_used_at: datetime | None) -> McpGrant:
    return McpGrant(
        user_id=1,
        client_id="client-1",
        client_name="Client",
        scopes=["mcp:read"],
        last_used_at=last_used_at,
    )


def _repo() -> tuple[McpGrantRepository, AsyncMock]:
    session = AsyncMock()
    session.add = MagicMock()
    return McpGrantRepository(session), session


@pytest.mark.asyncio
async def test_touch_skips_the_write_when_grant_was_used_moments_ago() -> None:
    recent = datetime.now(UTC) - GRANT_TOUCH_MIN_INTERVAL / 5
    repo, session = _repo()
    grant = _grant(recent)

    await repo.touch(grant)

    assert grant.last_used_at == recent
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_touch_writes_when_last_use_is_older_than_the_interval() -> None:
    stale = datetime.now(UTC) - GRANT_TOUCH_MIN_INTERVAL - timedelta(seconds=1)
    repo, session = _repo()
    grant = _grant(stale)

    await repo.touch(grant)

    assert grant.last_used_at is not None and grant.last_used_at > stale
    assert grant.updated_at == grant.last_used_at
    session.add.assert_called_once_with(grant)
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_touch_writes_the_first_time_a_grant_is_used() -> None:
    repo, session = _repo()
    grant = _grant(None)

    await repo.touch(grant)

    assert grant.last_used_at is not None
    session.flush.assert_awaited_once()
