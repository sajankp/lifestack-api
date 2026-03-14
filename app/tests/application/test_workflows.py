from unittest.mock import AsyncMock

import pytest

from app.application.workflows import UserRegistrationWorkflow
from app.auth.models import User
from app.auth.schemas import UserCreate


@pytest.fixture
def mock_auth_service():
    return AsyncMock()


@pytest.fixture
def mock_workspace_service():
    return AsyncMock()


@pytest.fixture
def workflow(mock_auth_service, mock_workspace_service):
    return UserRegistrationWorkflow(mock_auth_service, mock_workspace_service)


@pytest.mark.asyncio
async def test_register_user_with_workspace(workflow, mock_auth_service, mock_workspace_service):
    user_in = UserCreate(email="test@example.com", username="testuser", password="password123")
    mock_user = User(id=1, email="test@example.com", username="testuser", hashed_password="hashed")
    mock_auth_service.register_user.return_value = mock_user

    result = await workflow.register_user_with_workspace(user_in)

    assert result is True
    mock_auth_service.register_user.assert_called_once_with(user_in)
    mock_workspace_service.ensure_default_workspace.assert_called_once_with(
        user_id=1, username="testuser"
    )
