"""Unit tests for MCP tool-level workspace authorization (app.mcp.security)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import ForbiddenError
from app.mcp.security import authorize_workspace


def _token(scopes):
    return SimpleNamespace(scopes=set(scopes), claims={"user_id": "101"}, client_id="test-client")


def _membership(role):
    return SimpleNamespace(role=role)


@pytest.mark.anyio
async def test_write_scope_rejects_viewer_role():
    """A viewer-role member holding an mcp:write grant must not run write tools.

    REST mutation endpoints enforce require_min_role("member"); the MCP
    surface previously checked only that a membership row exists, so a viewer
    could execute create_transfer / delete_spending_transaction / delete_todo
    with an mcp:write grant.
    """
    session = MagicMock()
    with (
        patch("app.mcp.security.get_access_token", return_value=_token({"mcp:write"})),
        patch("app.mcp.security.MembershipRepository") as repo_cls,
    ):
        repo_cls.return_value.get_membership = AsyncMock(return_value=_membership("viewer"))

        with pytest.raises(ForbiddenError):
            await authorize_workspace(session, 202, required_scope="mcp:write", tool="create_todo")


@pytest.mark.anyio
async def test_write_scope_allows_member_role():
    session = MagicMock()
    with (
        patch("app.mcp.security.get_access_token", return_value=_token({"mcp:write"})),
        patch("app.mcp.security.MembershipRepository") as repo_cls,
    ):
        repo_cls.return_value.get_membership = AsyncMock(return_value=_membership("member"))

        user_id = await authorize_workspace(
            session, 202, required_scope="mcp:write", tool="create_todo"
        )
        assert user_id == 101


@pytest.mark.anyio
async def test_write_scope_allows_owner_role():
    session = MagicMock()
    with (
        patch("app.mcp.security.get_access_token", return_value=_token({"mcp:write"})),
        patch("app.mcp.security.MembershipRepository") as repo_cls,
    ):
        repo_cls.return_value.get_membership = AsyncMock(return_value=_membership("owner"))

        user_id = await authorize_workspace(
            session, 202, required_scope="mcp:write", tool="create_transfer"
        )
        assert user_id == 101


@pytest.mark.anyio
async def test_read_scope_allows_viewer_role():
    """Read tools stay membership-gated: viewers may read, mirroring REST."""
    session = MagicMock()
    with (
        patch("app.mcp.security.get_access_token", return_value=_token({"mcp:read"})),
        patch("app.mcp.security.MembershipRepository") as repo_cls,
    ):
        repo_cls.return_value.get_membership = AsyncMock(return_value=_membership("viewer"))

        user_id = await authorize_workspace(
            session, 202, required_scope="mcp:read", tool="list_todos"
        )
        assert user_id == 101


@pytest.mark.anyio
async def test_research_scope_rejects_viewer_role():
    """mcp:research gates two mutating tools, so it is a write scope.

    `write_investment_constituent_snapshot` and
    `delete_investment_constituent_snapshot` are the only tools gated on
    `mcp:research`, and the equivalent REST endpoint
    (POST /v1/investing/instruments/{id}/constituents) requires
    require_min_role("member"). Before this was fixed the role check only
    covered `mcp:write`, so a viewer holding an `mcp:research` grant could
    replace and delete constituent snapshots.
    """
    session = MagicMock()
    with (
        patch("app.mcp.security.get_access_token", return_value=_token({"mcp:research"})),
        patch("app.mcp.security.MembershipRepository") as repo_cls,
    ):
        repo_cls.return_value.get_membership = AsyncMock(return_value=_membership("viewer"))

        with pytest.raises(ForbiddenError):
            await authorize_workspace(
                session,
                202,
                required_scope="mcp:research",
                tool="write_investment_constituent_snapshot",
            )


@pytest.mark.anyio
async def test_research_scope_rejects_viewer_for_delete_tool():
    """The delete side of the research surface is gated the same way."""
    session = MagicMock()
    with (
        patch("app.mcp.security.get_access_token", return_value=_token({"mcp:research"})),
        patch("app.mcp.security.MembershipRepository") as repo_cls,
    ):
        repo_cls.return_value.get_membership = AsyncMock(return_value=_membership("viewer"))

        with pytest.raises(ForbiddenError):
            await authorize_workspace(
                session,
                202,
                required_scope="mcp:research",
                tool="delete_investment_constituent_snapshot",
            )


@pytest.mark.anyio
async def test_research_scope_allows_member_role():
    """A member holding an mcp:research grant may still use the research tools."""
    session = MagicMock()
    with (
        patch("app.mcp.security.get_access_token", return_value=_token({"mcp:research"})),
        patch("app.mcp.security.MembershipRepository") as repo_cls,
    ):
        repo_cls.return_value.get_membership = AsyncMock(return_value=_membership("member"))

        user_id = await authorize_workspace(
            session,
            202,
            required_scope="mcp:research",
            tool="write_investment_constituent_snapshot",
        )
        assert user_id == 101


@pytest.mark.anyio
async def test_research_scope_allows_owner_role():
    session = MagicMock()
    with (
        patch("app.mcp.security.get_access_token", return_value=_token({"mcp:research"})),
        patch("app.mcp.security.MembershipRepository") as repo_cls,
    ):
        repo_cls.return_value.get_membership = AsyncMock(return_value=_membership("owner"))

        user_id = await authorize_workspace(
            session,
            202,
            required_scope="mcp:research",
            tool="delete_investment_constituent_snapshot",
        )
        assert user_id == 101
