"""
E2E audit logging test for the Todo module.

Co-verification principle:
  Each assertion block opens ONE session and verifies BOTH the entity's DB state
  AND the audit log fields in the same pass. This proves the audit log is a
  faithful mirror of reality — not just that a log was written, but that what
  it records matches what is actually stored.

Full stack path: HTTP API → router → TodoService → TodoRepository + AuditLogger → DB.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.audit import AuditLog
from app.core.database import postgres
from app.todo.models import Todo


@pytest.mark.asyncio
async def test_todo_audit_logging_e2e(client: AsyncClient):
    # --- Setup: register and authenticate a real user ---
    user_data = {
        "email": "todo_audit_e2e@example.com",
        "username": "todo_audit_e2e",
        "password": "todo_audit_password",
    }
    reg_res = await client.post("/v1/auth/register", json=user_data)
    assert reg_res.status_code == 200

    login_res = await client.post(
        "/v1/auth/login",
        data={"username": user_data["username"], "password": user_data["password"]},
    )
    assert login_res.status_code == 200
    cookies = dict(login_res.cookies)

    # --- Create ---
    todo_payload = {
        "title": "E2E Audit Todo",
        "description": "Verify audit works",
        "priority": "medium",
    }
    create_res = await client.post("/v1/todo/", json=todo_payload, cookies=cookies)
    assert create_res.status_code == 201
    api_todo = create_res.json()
    todo_uuid = api_todo["public_id"]

    # Co-verify: DB entity state AND audit log in one session
    async with postgres.async_session_maker() as session:
        db_todo = (
            await session.execute(select(Todo).where(Todo.public_id == todo_uuid))
        ).scalar_one()

        # Entity is correctly persisted
        assert db_todo.title == "E2E Audit Todo"
        assert db_todo.description == "Verify audit works"
        assert db_todo.priority == "medium"
        assert db_todo.completed is False

        # API response matches DB (no field silently mangled between layers)
        assert api_todo["title"] == db_todo.title
        assert api_todo["completed"] == db_todo.completed

        # Audit log faithfully mirrors the DB entity — not just the request payload
        audit = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.entity_id == db_todo.id)
                .where(AuditLog.entity_type == "todo")
                .order_by(AuditLog.timestamp.asc())
            )
        ).scalar_one()  # Exactly one log entry after create

        assert audit.action == "create"
        assert audit.module == "todo"
        assert audit.entity_id == db_todo.id
        assert audit.details["entity_public_id"] == str(db_todo.public_id)
        assert audit.details["before"] is None  # create contract
        assert audit.details["after"]["title"] == db_todo.title  # mirrors DB
        assert audit.details["after"]["completed"] == db_todo.completed
        assert "title" in audit.details["changed_fields"]  # non-empty for create

        todo_id = db_todo.id  # Capture for subsequent steps

    # --- Update ---
    update_payload = {"title": "E2E Updated Audit Todo"}
    update_res = await client.patch(f"/v1/todo/{todo_uuid}", json=update_payload, cookies=cookies)
    assert update_res.status_code == 200
    api_updated = update_res.json()

    async with postgres.async_session_maker() as session:
        db_todo = (await session.execute(select(Todo).where(Todo.id == todo_id))).scalar_one()

        # Entity is updated in DB
        assert db_todo.title == "E2E Updated Audit Todo"

        # API response matches DB
        assert api_updated["title"] == db_todo.title

        # Audit log records the delta faithfully
        logs = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_id == todo_id)
                    .order_by(AuditLog.timestamp.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 2

        update_log = logs[1]
        assert update_log.action == "update"
        assert update_log.details["before"]["title"] == "E2E Audit Todo"  # old DB value
        assert update_log.details["after"]["title"] == db_todo.title  # mirrors current DB
        assert update_log.details["changed_fields"] == ["title"]

    # --- Complete ---
    complete_res = await client.patch(
        f"/v1/todo/{todo_uuid}", json={"completed": True}, cookies=cookies
    )
    assert complete_res.status_code == 200
    api_completed = complete_res.json()

    async with postgres.async_session_maker() as session:
        db_todo = (await session.execute(select(Todo).where(Todo.id == todo_id))).scalar_one()

        # Entity is marked completed in DB
        assert db_todo.completed is True

        # API response matches DB
        assert api_completed["completed"] == db_todo.completed

        # Audit action is "complete" (not "update") and mirrors DB state
        logs = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_id == todo_id)
                    .order_by(AuditLog.timestamp.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 3

        complete_log = logs[2]
        assert complete_log.action == "complete"
        assert complete_log.details["before"]["completed"] is False
        assert complete_log.details["after"]["completed"] == db_todo.completed  # mirrors DB

    # --- Delete ---
    # Capture title before deletion for before-snapshot verification
    async with postgres.async_session_maker() as session:
        db_todo = (await session.execute(select(Todo).where(Todo.id == todo_id))).scalar_one()
        title_before_delete = db_todo.title

    delete_res = await client.delete(f"/v1/todo/{todo_uuid}", cookies=cookies)
    assert delete_res.status_code == 204

    async with postgres.async_session_maker() as session:
        # Entity is gone from DB
        gone = (await session.execute(select(Todo).where(Todo.id == todo_id))).scalar()
        assert gone is None

        # Audit log preserves the before-state and records null after
        logs = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_id == todo_id)
                    .order_by(AuditLog.timestamp.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 4

        delete_log = logs[3]
        assert delete_log.action == "delete"
        assert delete_log.details["before"]["title"] == title_before_delete  # last known DB value
        assert delete_log.details["after"] is None  # delete contract
        assert delete_log.details["changed_fields"] == []  # no field diff for delete
