from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.auth.models import User
from app.core.database import postgres
from app.platform.models import Workspace, WorkspaceMembership, WorkspaceRole
from app.spending.models import SpendingCategory


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


@pytest.mark.asyncio
async def test_registration_seeds_default_spending_categories(client: AsyncClient):
    """Registration must atomically seed system spending categories for the new workspace."""
    email = "catuser@example.com"
    username = "catuser"
    resp = await client.post(
        "/v1/auth/register",
        json={"email": email, "username": username, "password": "testpassword"},
    )
    assert resp.status_code == 200

    async with postgres.async_session_maker() as session:
        workspace_result = await session.execute(
            select(Workspace).where(Workspace.name == f"{username}'s Workspace")
        )
        workspace = workspace_result.scalar_one()

        cat_result = await session.execute(
            select(SpendingCategory).where(SpendingCategory.workspace_id == workspace.id)
        )
        categories = cat_result.scalars().all()

    assert len(categories) >= 8, f"Expected at least 8 default categories, got {len(categories)}"
    assert all(c.is_system for c in categories), "All default categories must be system categories"
    assert all(c.workspace_id == workspace.id for c in categories)


@pytest.mark.asyncio
async def test_registration_rollback_on_category_failure(client: AsyncClient):
    """If category provisioning fails mid-workflow, the entire registration must roll back.

    This verifies the atomicity guarantee: no orphaned user, workspace, or membership
    rows should persist when a downstream step fails.
    """
    email = "rollback@example.com"
    username = "rollbackuser"

    with (
        patch(
            "app.spending.service.CategoryService.provision_default_categories",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Simulated category provisioning failure"),
        ),
        pytest.raises(RuntimeError, match="Simulated category provisioning failure"),
    ):
        await client.post(
            "/v1/auth/register",
            json={"email": email, "username": username, "password": "testpassword"},
        )

    # After the rollback, none of the registration artifacts should exist
    async with postgres.async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.username == username))
        assert (
            user_result.scalar_one_or_none() is None
        ), "User row must not persist after a failed registration workflow"

        workspace_result = await session.execute(
            select(Workspace).where(Workspace.name == f"{username}'s Workspace")
        )
        assert (
            workspace_result.scalar_one_or_none() is None
        ), "Workspace row must not persist after a failed registration workflow"

        membership_result = await session.execute(
            select(WorkspaceMembership)
            .join(User, WorkspaceMembership.user_id == User.id)
            .where(User.email == email)
        )
        assert (
            membership_result.scalar_one_or_none() is None
        ), "Membership row must not persist after a failed registration workflow"
