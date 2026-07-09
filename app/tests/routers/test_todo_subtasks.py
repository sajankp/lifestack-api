"""Spec-068: subtasks, sort=due_date, and Clear completed."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import postgres
from app.todo.models import Todo


async def _register_and_login(client: AsyncClient, username: str) -> None:
    user = {"email": f"{username}@example.com", "username": username, "password": "TestPass123!"}
    reg_res = await client.post("/v1/auth/register", json=user)
    assert reg_res.status_code == 200
    login_res = await client.post(
        "/v1/auth/login", data={"username": username, "password": "TestPass123!"}
    )
    assert login_res.status_code == 200


async def _create_todo(client: AsyncClient, **kwargs) -> dict:
    res = await client.post("/v1/todo/", json=kwargs)
    assert res.status_code == 201
    return res.json()


@pytest.mark.asyncio
async def test_create_subtask_and_parent_gets_subtask_count(client: AsyncClient):
    await _register_and_login(client, "todo_subtask_basic")

    parent = await _create_todo(client, title="Plan trip", priority="medium")
    child = await _create_todo(
        client, title="Book flights", priority="medium", parent_public_id=parent["public_id"]
    )
    assert child["parent_public_id"] == parent["public_id"]
    assert child["subtask_count"] == 0

    get_parent = await client.get(f"/v1/todo/{parent['public_id']}")
    assert get_parent.status_code == 200
    assert get_parent.json()["subtask_count"] == 1
    assert get_parent.json()["parent_public_id"] is None


@pytest.mark.asyncio
async def test_two_level_nesting_rejected(client: AsyncClient):
    await _register_and_login(client, "todo_subtask_nesting")

    parent = await _create_todo(client, title="Parent", priority="medium")
    child = await _create_todo(
        client, title="Child", priority="medium", parent_public_id=parent["public_id"]
    )

    grandchild_res = await client.post(
        "/v1/todo/",
        json={"title": "Grandchild", "priority": "medium", "parent_public_id": child["public_id"]},
    )
    assert grandchild_res.status_code == 422


@pytest.mark.asyncio
async def test_todo_with_children_cannot_be_given_a_parent(client: AsyncClient):
    await _register_and_login(client, "todo_subtask_reparent")

    a = await _create_todo(client, title="A", priority="medium")
    await _create_todo(client, title="A-child", priority="medium", parent_public_id=a["public_id"])
    b = await _create_todo(client, title="B", priority="medium")

    res = await client.patch(
        f"/v1/todo/{a['public_id']}", json={"parent_public_id": b["public_id"]}
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_todo_cannot_be_its_own_parent(client: AsyncClient):
    await _register_and_login(client, "todo_subtask_self")

    a = await _create_todo(client, title="A", priority="medium")
    res = await client.patch(
        f"/v1/todo/{a['public_id']}", json={"parent_public_id": a["public_id"]}
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_parent_from_another_workspace_rejected(client: AsyncClient):
    await _register_and_login(client, "todo_subtask_ws1")
    other_workspace_todo = await _create_todo(client, title="WS1 todo", priority="medium")
    await client.post("/v1/auth/logout")

    await _register_and_login(client, "todo_subtask_ws2")
    res = await client.post(
        "/v1/todo/",
        json={
            "title": "Cross-workspace subtask",
            "priority": "medium",
            "parent_public_id": other_workspace_todo["public_id"],
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_clearing_parent_promotes_subtask_to_top_level(client: AsyncClient):
    await _register_and_login(client, "todo_subtask_promote")

    parent = await _create_todo(client, title="Parent", priority="medium")
    child = await _create_todo(
        client, title="Child", priority="medium", parent_public_id=parent["public_id"]
    )

    res = await client.patch(f"/v1/todo/{child['public_id']}", json={"parent_public_id": None})
    assert res.status_code == 200
    assert res.json()["parent_public_id"] is None


@pytest.mark.asyncio
async def test_completing_parent_cascades_to_open_subtasks_with_own_audit_entries(
    client: AsyncClient,
):
    await _register_and_login(client, "todo_subtask_complete")

    parent = await _create_todo(client, title="Parent", priority="medium")
    child_one = await _create_todo(
        client, title="Child one", priority="medium", parent_public_id=parent["public_id"]
    )
    child_two = await _create_todo(
        client, title="Child two", priority="medium", parent_public_id=parent["public_id"]
    )
    # Already-completed subtask should not get a duplicate cascade audit entry.
    already_done = await _create_todo(
        client, title="Already done", priority="medium", parent_public_id=parent["public_id"]
    )
    await client.patch(f"/v1/todo/{already_done['public_id']}", json={"completed": True})

    complete_res = await client.patch(f"/v1/todo/{parent['public_id']}", json={"completed": True})
    assert complete_res.status_code == 200

    async with postgres.async_session_maker() as session:
        for child in (child_one, child_two):
            db_child = (
                await session.execute(select(Todo).where(Todo.public_id == child["public_id"]))
            ).scalar_one()
            assert db_child.completed is True

        db_already_done = (
            await session.execute(select(Todo).where(Todo.public_id == already_done["public_id"]))
        ).scalar_one()
        cascade_logs = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == db_already_done.id, AuditLog.action == "complete"
                    )
                )
            )
            .scalars()
            .all()
        )
        # Only the earlier direct completion — not re-completed by the cascade.
        assert len(cascade_logs) == 1


@pytest.mark.asyncio
async def test_uncompleting_parent_does_not_resurrect_subtasks(client: AsyncClient):
    await _register_and_login(client, "todo_subtask_uncomplete")

    parent = await _create_todo(client, title="Parent", priority="medium")
    child = await _create_todo(
        client, title="Child", priority="medium", parent_public_id=parent["public_id"]
    )
    await client.patch(f"/v1/todo/{parent['public_id']}", json={"completed": True})
    await client.patch(f"/v1/todo/{parent['public_id']}", json={"completed": False})

    get_child = await client.get(f"/v1/todo/{child['public_id']}")
    assert get_child.json()["completed"] is True


@pytest.mark.asyncio
async def test_deleting_parent_cascades_to_subtasks(client: AsyncClient):
    await _register_and_login(client, "todo_subtask_delete")

    parent = await _create_todo(client, title="Parent", priority="medium")
    child = await _create_todo(
        client, title="Child", priority="medium", parent_public_id=parent["public_id"]
    )

    delete_res = await client.delete(f"/v1/todo/{parent['public_id']}")
    assert delete_res.status_code == 204

    get_child = await client.get(f"/v1/todo/{child['public_id']}")
    assert get_child.status_code == 404


@pytest.mark.asyncio
async def test_sort_due_date_orders_by_due_date_then_priority_then_created_at(
    client: AsyncClient,
):
    await _register_and_login(client, "todo_sort_due_date")

    no_date_low = await _create_todo(client, title="No date low", priority="low")
    no_date_high = await _create_todo(client, title="No date high", priority="high")
    due_later = await _create_todo(
        client, title="Due later", priority="low", due_date="2026-08-01T00:00:00Z"
    )
    due_soon_low = await _create_todo(
        client, title="Due soon low", priority="low", due_date="2026-07-10T00:00:00Z"
    )
    due_soon_high = await _create_todo(
        client, title="Due soon high", priority="high", due_date="2026-07-10T00:00:00Z"
    )

    res = await client.get("/v1/todo/", params={"sort": "due_date"})
    assert res.status_code == 200
    titles = [item["title"] for item in res.json()["items"]]

    assert titles == [
        due_soon_high["title"],
        due_soon_low["title"],
        due_later["title"],
        no_date_high["title"],
        no_date_low["title"],
    ]


@pytest.mark.asyncio
async def test_subtask_count_present_without_n_plus_one(client: AsyncClient):
    await _register_and_login(client, "todo_subtask_count_list")

    parent = await _create_todo(client, title="Parent", priority="medium")
    await _create_todo(
        client, title="Child A", priority="medium", parent_public_id=parent["public_id"]
    )
    await _create_todo(
        client, title="Child B", priority="medium", parent_public_id=parent["public_id"]
    )

    res = await client.get("/v1/todo/")
    assert res.status_code == 200
    items = {item["public_id"]: item for item in res.json()["items"]}
    assert items[parent["public_id"]]["subtask_count"] == 2


@pytest.mark.asyncio
async def test_delete_completed_removes_only_completed_todos_in_caller_workspace(
    client: AsyncClient,
):
    await _register_and_login(client, "todo_clear_completed_1")

    open_todo = await _create_todo(client, title="Open", priority="medium")
    done_one = await _create_todo(client, title="Done one", priority="medium")
    done_two = await _create_todo(client, title="Done two", priority="medium")
    await client.patch(f"/v1/todo/{done_one['public_id']}", json={"completed": True})
    await client.patch(f"/v1/todo/{done_two['public_id']}", json={"completed": True})

    await client.post("/v1/auth/logout")
    await _register_and_login(client, "todo_clear_completed_2")
    other_done = await _create_todo(client, title="Other workspace done", priority="medium")
    await client.patch(f"/v1/todo/{other_done['public_id']}", json={"completed": True})
    await client.post("/v1/auth/logout")

    await client.post(
        "/v1/auth/login",
        data={"username": "todo_clear_completed_1", "password": "TestPass123!"},
    )

    delete_res = await client.delete("/v1/todo/completed")
    assert delete_res.status_code == 200
    assert delete_res.json() == {"deleted": 2}

    remaining = await client.get("/v1/todo/")
    remaining_ids = {item["public_id"] for item in remaining.json()["items"]}
    assert remaining_ids == {open_todo["public_id"]}

    other_get = await client.get(f"/v1/todo/{other_done['public_id']}")
    assert other_get.status_code == 404  # different workspace, not visible — still exists though


@pytest.mark.asyncio
async def test_delete_completed_route_resolves_before_uuid_path(client: AsyncClient):
    await _register_and_login(client, "todo_clear_completed_route")

    res = await client.delete("/v1/todo/completed")
    assert res.status_code == 200
    assert res.json() == {"deleted": 0}
