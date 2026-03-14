from unittest.mock import AsyncMock

import pytest

from app.platform.models import Workspace, WorkspaceRole
from app.platform.service import WorkspaceService


@pytest.fixture
def mock_ws_repo():
    return AsyncMock()


@pytest.fixture
def mock_member_repo():
    return AsyncMock()


@pytest.fixture
def service(mock_ws_repo, mock_member_repo):
    return WorkspaceService(mock_ws_repo, mock_member_repo)


@pytest.mark.asyncio
async def test_create_workspace(service, mock_ws_repo, mock_member_repo):
    mock_ws = Workspace(id=1, name="Test WS")
    mock_ws_repo.create.return_value = mock_ws

    result = await service.create_workspace(name="Test WS", user_id=10)

    assert result == mock_ws
    mock_ws_repo.create.assert_called_once()
    # Should also create membership for owner
    mock_member_repo.create.assert_called_once()
    member = mock_member_repo.create.call_args[0][0]
    assert member.workspace_id == 1
    assert member.user_id == 10
    assert member.role == WorkspaceRole.OWNER


@pytest.mark.asyncio
async def test_get_user_workspaces(service, mock_ws_repo):
    mock_ws = Workspace(id=1, name="Default")
    mock_ws_repo.list_user_workspaces.return_value = [mock_ws]

    result = await service.get_user_workspaces(user_id=10)

    assert len(result) == 1
    assert result[0] == mock_ws
    mock_ws_repo.list_user_workspaces.assert_called_once_with(10)


@pytest.mark.asyncio
async def test_ensure_default_workspace_exists(service, mock_ws_repo):
    # Case: Already has workspace
    mock_ws = Workspace(id=1, name="Existing")
    mock_ws_repo.list_user_workspaces.return_value = [mock_ws]

    result = await service.ensure_default_workspace(user_id=10, username="testuser")

    assert result == mock_ws
    mock_ws_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_default_workspace_creation(service, mock_ws_repo, mock_member_repo):
    # Case: No workspace
    mock_ws_repo.list_user_workspaces.return_value = []
    mock_ws = Workspace(id=2, name="testuser's Workspace")
    mock_ws_repo.create.return_value = mock_ws

    result = await service.ensure_default_workspace(user_id=10, username="testuser")

    assert result == mock_ws
    mock_ws_repo.create.assert_called_once()
