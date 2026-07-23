import pytest
from httpx import AsyncClient

from app.auth.repository import UserRepository
from app.capture import router as capture_router
from app.capture.router import authenticate_ws
from app.core.database import postgres
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.platform.models import WorkspaceRole
from app.platform.repository import MembershipRepository, WorkspaceRepository
from app.tests.integration.test_spending import _register_and_login


class MockWebSocket:
    def __init__(self, cookies=None, query_params=None):
        self.cookies = cookies or {}
        self.query_params = query_params or {}


@pytest.mark.asyncio
async def test_websocket_auth_cookie_only(client: AsyncClient):
    # Register and log in user
    creds = await _register_and_login(client, "wsauth")
    cookies = creds["cookies"]
    token = cookies.get("access_token")

    # 1. Test connection with valid cookie - should succeed
    mock_ws_valid = MockWebSocket(cookies={"access_token": token})
    user_id, workspace_id = await authenticate_ws(mock_ws_valid)  # type: ignore
    assert user_id > 0
    assert workspace_id > 0

    # 2. Test connection with query parameter token but NO cookie - should fail (cookie-only enforced)
    mock_ws_query = MockWebSocket(query_params={"token": token})
    with pytest.raises(UnauthorizedError) as exc_info:
        await authenticate_ws(mock_ws_query)  # type: ignore
    assert "Missing authorization token" in str(exc_info.value.detail)

    # 3. Test connection with no cookie and no query param - should fail
    mock_ws_none = MockWebSocket()
    with pytest.raises(UnauthorizedError) as exc_info:
        await authenticate_ws(mock_ws_none)  # type: ignore
    assert "Missing authorization token" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_websocket_auth_role_restriction(client: AsyncClient):
    # Register and log in user
    creds = await _register_and_login(client, "wsrole")
    cookies = creds["cookies"]
    token = cookies.get("access_token")

    async_session_maker = postgres.get_session_maker(postgres.engine)
    async with async_session_maker() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_username(creds["username"])
        assert user is not None
        user_id = user.id

        workspace_repo = WorkspaceRepository(session)
        workspaces = await workspace_repo.list_user_workspaces(user_id)
        workspace_id = workspaces[0].id

        # Update user's role to VIEWER in the workspace to test restriction
        membership_repo = MembershipRepository(session)
        membership = await membership_repo.get_membership(workspace_id, user_id)
        assert membership is not None
        membership.role = WorkspaceRole.VIEWER
        session.add(membership)
        await session.commit()

    # Try to authenticate with the viewer membership - should raise ForbiddenError
    mock_ws = MockWebSocket(cookies={"access_token": token})
    with pytest.raises(ForbiddenError) as exc_info:
        await authenticate_ws(mock_ws)  # type: ignore
    assert "Insufficient workspace permissions" in str(exc_info.value.detail)


class RecordingWebSocket:
    """Records accept/close calls so tests can assert the close-code contract."""

    def __init__(self):
        self.calls: list[object] = []
        self.query_params: dict[str, str] = {}

    async def accept(self):
        self.calls.append("accept")

    async def close(self, code: int | None = None):
        self.calls.append(("close", code))


@pytest.mark.asyncio
async def test_ws_endpoint_closes_4003_on_forbidden(monkeypatch):
    """Authorization (role) rejections must accept the handshake and close 4003.

    A pre-accept close never reaches the browser as its own code — the
    handshake just fails and the client sees 1006, which the spec-079 Stage B
    web client treats as a transient drop and retries five times. 4003 is in
    the client's no-retry set, so a forbidden viewer stops immediately.
    """

    async def raise_forbidden(ws):
        raise ForbiddenError(detail="Insufficient workspace permissions")

    monkeypatch.setattr(capture_router, "authenticate_ws", raise_forbidden)
    ws = RecordingWebSocket()
    await capture_router.websocket_agent_endpoint(ws)  # type: ignore[arg-type]
    assert ws.calls == ["accept", ("close", 4003)]


@pytest.mark.asyncio
async def test_ws_endpoint_closes_4001_on_unauthorized(monkeypatch):
    """Authentication failures keep the pre-accept 4001 close (no accept)."""

    async def raise_unauthorized(ws):
        raise UnauthorizedError(detail="Missing authorization token")

    monkeypatch.setattr(capture_router, "authenticate_ws", raise_unauthorized)
    ws = RecordingWebSocket()
    await capture_router.websocket_agent_endpoint(ws)  # type: ignore[arg-type]
    assert ws.calls == [("close", 4001)]
