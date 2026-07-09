import uuid
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import NotFoundError
from app.todo.models import Todo
from app.todo.schemas import TodoCreate, TodoUpdate
from app.todo.service import TodoService


@pytest.fixture
def mock_repo():
    return AsyncMock()


@pytest.fixture
def service(mock_repo):
    return TodoService(repository=mock_repo)


@pytest.mark.asyncio
async def test_list_todos(service, mock_repo):
    mock_repo.get_all.return_value = ([], 0)
    result = await service.list_todos(workspace_id=1, completed=True)
    mock_repo.get_all.assert_called_once_with(1, True, 50, 0, "created_at")
    assert result == ([], 0)


@pytest.mark.asyncio
async def test_get_todo_success(service, mock_repo):
    todo_id = uuid.uuid4()
    mock_todo = Todo(id=1, public_id=todo_id, workspace_id=1, user_id=1, title="Test")
    mock_repo.get_by_public_id.return_value = mock_todo

    result = await service.get_todo(workspace_id=1, public_id=todo_id)

    mock_repo.get_by_public_id.assert_called_once_with(1, todo_id)
    assert result == mock_todo


@pytest.mark.asyncio
async def test_get_todo_not_found(service, mock_repo):
    todo_id = uuid.uuid4()
    mock_repo.get_by_public_id.return_value = None

    with pytest.raises(NotFoundError) as exc:
        await service.get_todo(workspace_id=1, public_id=todo_id)

    assert exc.value.status_code == 404
    assert "not found" in exc.value.detail


@pytest.mark.asyncio
async def test_create_todo(service, mock_repo):
    todo_in = TodoCreate(title="New Todo", description="Desc")
    mock_repo.create.return_value = Todo(id=1, workspace_id=1, user_id=1, **todo_in.model_dump())

    result = await service.create_todo(user_id=1, workspace_id=1, todo_in=todo_in)

    mock_repo.create.assert_called_once()
    assert result.title == "New Todo"
    assert result.workspace_id == 1


@pytest.mark.asyncio
async def test_update_todo_success(service, mock_repo):
    todo_id = uuid.uuid4()
    mock_todo = Todo(id=1, public_id=todo_id, workspace_id=1, user_id=1, title="Old Title")
    mock_repo.get_by_public_id.return_value = mock_todo
    mock_repo.save.side_effect = lambda x: x

    todo_update = TodoUpdate(title="New Title", completed=True)
    result = await service.update_todo(workspace_id=1, public_id=todo_id, todo_in=todo_update)

    assert result.title == "New Title"
    assert result.completed is True
    mock_repo.save.assert_called_once()


@pytest.mark.asyncio
async def test_delete_todo_success(service, mock_repo):
    todo_id = uuid.uuid4()
    mock_todo = Todo(id=1, public_id=todo_id, workspace_id=1, user_id=1, title="Delete Me")
    mock_repo.get_by_public_id.return_value = mock_todo

    await service.delete_todo(workspace_id=1, public_id=todo_id)

    mock_repo.delete.assert_called_once_with(mock_todo)
