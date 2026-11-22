import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.core.exceptions import NotFoundError, ValidationError
from app.todo.models import Todo
from app.todo.schemas import RecurringTodoRuleCreate, TodoCreate, TodoResponse, TodoUpdate
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
    mock_repo.get_all.assert_called_once_with(1, True, None, 50, 0, "created_at")
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
    todo_in = TodoCreate(title="New Todo", description="Desc", category="Finance")
    mock_repo.create.return_value = Todo(id=1, workspace_id=1, user_id=1, **todo_in.model_dump())

    result = await service.create_todo(user_id=1, workspace_id=1, todo_in=todo_in)

    mock_repo.create.assert_called_once()
    assert result.title == "New Todo"
    assert result.category == "Finance"
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


# ---------------------------------------------------------------------------
# Sub-todo (parent_public_id) tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_todo_with_parent(service, mock_repo):
    parent_public_id = uuid.uuid4()
    parent = Todo(id=7, public_id=parent_public_id, workspace_id=1, user_id=1, title="Parent")
    mock_repo.get_by_public_id.return_value = parent
    mock_repo.create.side_effect = lambda todo: todo

    result = await service.create_todo(
        user_id=1,
        workspace_id=1,
        todo_in=TodoCreate(title="Sub-task", parent_public_id=parent_public_id),
    )

    mock_repo.get_by_public_id.assert_called_once_with(1, parent_public_id)
    created = mock_repo.create.call_args.args[0]
    assert created.parent_id == 7
    assert result.parent_id == 7


@pytest.mark.asyncio
async def test_create_todo_parent_from_another_workspace_rejected(service, mock_repo):
    # get_by_public_id is workspace-scoped: a todo in another workspace is not found.
    mock_repo.get_by_public_id.return_value = None

    with pytest.raises(ValidationError) as exc:
        await service.create_todo(
            user_id=1,
            workspace_id=1,
            todo_in=TodoCreate(title="Sub-task", parent_public_id=uuid.uuid4()),
        )

    assert "not found" in exc.value.detail


@pytest.mark.asyncio
async def test_create_todo_two_level_parent_rejected(service, mock_repo):
    # A subtask cannot itself be a parent (one level only).
    parent = Todo(
        id=7, public_id=uuid.uuid4(), workspace_id=1, user_id=1, title="Parent", parent_id=3
    )
    mock_repo.get_by_public_id.return_value = parent

    with pytest.raises(ValidationError) as exc:
        await service.create_todo(
            user_id=1,
            workspace_id=1,
            todo_in=TodoCreate(title="Grandchild", parent_public_id=parent.public_id),
        )

    assert "one level only" in exc.value.detail


@pytest.mark.asyncio
async def test_update_todo_detach_parent(service, mock_repo):
    todo_id = uuid.uuid4()
    todo = Todo(id=1, public_id=todo_id, workspace_id=1, user_id=1, title="Task", parent_id=5)
    mock_repo.get_by_public_id.return_value = todo
    mock_repo.save.side_effect = lambda x: x

    result = await service.update_todo(
        workspace_id=1,
        public_id=todo_id,
        todo_in=TodoUpdate.model_validate({"parent_public_id": None}),
    )

    assert todo.parent_id is None
    assert result.parent_id is None


@pytest.mark.asyncio
async def test_update_todo_reparent_validates_one_level(service, mock_repo):
    todo_id = uuid.uuid4()
    todo = Todo(id=1, public_id=todo_id, workspace_id=1, user_id=1, title="Task")
    parent = Todo(
        id=7, public_id=uuid.uuid4(), workspace_id=1, user_id=1, title="Parent", parent_id=3
    )
    mock_repo.get_by_public_id.return_value = parent
    mock_repo.get_child_count.return_value = 0
    mock_repo.save.side_effect = lambda x: x

    with pytest.raises(ValidationError) as exc:
        await service.update_todo(
            workspace_id=1,
            public_id=todo_id,
            todo_in=TodoUpdate.model_validate({"parent_public_id": parent.public_id}),
        )

    assert "one level only" in exc.value.detail
    assert todo.parent_id is None


@pytest.mark.asyncio
async def test_update_todo_parent_with_children_rejected(service, mock_repo):
    todo_id = uuid.uuid4()
    todo = Todo(id=1, public_id=todo_id, workspace_id=1, user_id=1, title="Task")
    mock_repo.get_by_public_id.return_value = todo
    mock_repo.get_child_count.return_value = 1  # this todo already has subtasks
    parent = Todo(id=7, public_id=uuid.uuid4(), workspace_id=1, user_id=1, title="Parent")
    mock_repo.get_by_public_id.return_value = todo  # first call: the todo itself
    # second get_by_public_id call resolves the candidate parent
    mock_repo.get_by_public_id.side_effect = [todo, parent]

    with pytest.raises(ValidationError) as exc:
        await service.update_todo(
            workspace_id=1,
            public_id=todo_id,
            todo_in=TodoUpdate.model_validate({"parent_public_id": parent.public_id}),
        )

    assert "subtasks cannot itself be made a subtask" in exc.value.detail


@pytest.mark.asyncio
async def test_update_todo_parent_from_another_workspace_rejected(service, mock_repo):
    todo_id = uuid.uuid4()
    todo = Todo(id=1, public_id=todo_id, workspace_id=1, user_id=1, title="Task")
    mock_repo.get_by_public_id.side_effect = [
        todo,
        None,
    ]  # the todo exists; the candidate parent is not in this workspace
    mock_repo.get_child_count.return_value = 0

    with pytest.raises(ValidationError) as exc:
        await service.update_todo(
            workspace_id=1,
            public_id=todo_id,
            todo_in=TodoUpdate.model_validate({"parent_public_id": uuid.uuid4()}),
        )

    assert "not found" in exc.value.detail


@pytest.mark.asyncio
async def test_to_response_returns_parent_public_id_and_subtask_count(service, mock_repo):
    parent_public_id = uuid.uuid4()
    todo = Todo(id=1, public_id=uuid.uuid4(), workspace_id=1, user_id=1, title="Task", parent_id=5)
    mock_repo.get_child_count.return_value = 3
    mock_repo.get_public_ids_by_ids.return_value = {5: parent_public_id}

    response = await service.to_response(workspace_id=1, todo=todo)

    assert isinstance(response, TodoResponse)
    assert response.parent_public_id == parent_public_id
    assert response.subtask_count == 3


@pytest.mark.asyncio
async def test_list_todos_returns_parent_public_id_and_subtask_count(service, mock_repo):
    parent_public_id = uuid.uuid4()
    items = [Todo(id=1, public_id=uuid.uuid4(), workspace_id=1, user_id=1, title="A", parent_id=5)]
    mock_repo.get_all.return_value = (items, 1)
    mock_repo.get_subtask_counts.return_value = {1: 1}
    mock_repo.get_public_ids_by_ids.return_value = {5: parent_public_id}

    response, total = await service.list_todos(workspace_id=1)

    assert total == 1
    assert response[0].parent_public_id == parent_public_id
    assert response[0].subtask_count == 1
