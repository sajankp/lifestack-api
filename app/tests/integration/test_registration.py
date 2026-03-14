import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.auth.models import User
from app.core.database import postgres
from app.platform.models import Workspace, WorkspaceMembership, WorkspaceRole


@pytest.mark.asyncio
async def test_registration_creates_required_entities(client: AsyncClient):
    # Register
    email = "newuser@example.com"
    username = "newuser"
    register_response = await client.post(
        "/v1/auth/register",
        json={"email": email, "username": username, "password": "testpassword"},
    )
    assert register_response.status_code == 200
    assert register_response.json() is True

    # Verify Database State
    async with postgres.async_session_maker() as session:
        # Check User
        user_result = await session.execute(select(User).where(User.username == username))
        user = user_result.scalar_one_or_none()
        assert user is not None
        assert user.email == email

        # Check Workspace
        # Registration workflow: f"{username}'s Workspace"
        workspace_result = await session.execute(
            select(Workspace).where(Workspace.name == f"{username}'s Workspace")
        )
        workspace = workspace_result.scalar_one_or_none()
        assert workspace is not None

        # Check Membership
        membership_result = await session.execute(
            select(WorkspaceMembership)
            .where(WorkspaceMembership.user_id == user.id)
            .where(WorkspaceMembership.workspace_id == workspace.id)
        )
        membership = membership_result.scalar_one_or_none()
        assert membership is not None
        assert membership.role == WorkspaceRole.OWNER
