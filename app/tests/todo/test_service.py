import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.core.exceptions import NotFoundError
from app.todo.models import Todo
from app.todo.schemas import RecurringTodoRuleCreate, TodoCreate, TodoUpdate
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


@pytest.mark.asyncio
async def test_create_recurring_rule_uses_rule_timezone_not_utc(service, mock_repo):
    """A rule created late in the evening in a negative-UTC-offset timezone must
    compute "today" in the rule's own timezone, not UTC — otherwise UTC "today"
    is already the next local day and the first cycle gets skipped."""
    mock_repo.create_recurring_rule.side_effect = lambda x: x

    # Fixed instant: 2026-07-01 04:30 UTC == 2026-06-30 21:30 in
    # America/Los_Angeles (PDT, UTC-7) — UTC's calendar date is already a day
    # ahead of the rule owner's local calendar date.
    fixed_instant = datetime(2026, 7, 1, 4, 30, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_instant.astimezone(tz) if tz else fixed_instant

    rule_in = RecurringTodoRuleCreate(
        title="Evening rule",
        frequency="monthly",
        interval=1,
        anchor_date=date(2026, 6, 30),
        timezone="America/Los_Angeles",
    )

    with patch("app.todo.service.datetime", FixedDatetime):
        rule = await service.create_recurring_rule(user_id=1, workspace_id=1, rule_in=rule_in)

    # Local "today" is still 2026-06-30, matching anchor_date exactly — not yet
    # elapsed, so next_due_date must stay put, not skip ahead to next month.
    assert rule.next_due_date == date(2026, 6, 30)
