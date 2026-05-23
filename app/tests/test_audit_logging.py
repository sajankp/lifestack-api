import uuid

import pytest
from sqlalchemy import select

# These imports will fail initially as we haven't implemented app.core.audit yet
from app.core.audit import AuditLog, AuditLogger, redact_details
from app.core.database.postgres import async_session_maker
from app.todo.repository import TodoRepository
from app.todo.schemas import TodoCreate, TodoUpdate
from app.todo.service import TodoService


@pytest.mark.asyncio
async def test_redact_details_sensitive_keys():
    """Verify that sensitive keys are recursively redacted case-insensitively."""
    raw_payload = {
        "user_id": 42,
        "password": "my-super-secret-password",
        "api_key": "sk-123456",
        "nested": {
            "token": "bearer-token-here",
            "safe_key": "safe-value",
            "CREDENTIALS": {"secret_value": "very-secret"},
        },
        "account_number": "1234-5678-9012",
    }

    redacted = redact_details(raw_payload)

    assert redacted["user_id"] == 42
    assert redacted["password"] == "[REDACTED]"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["token"] == "[REDACTED]"
    assert redacted["nested"]["safe_key"] == "safe-value"
    assert redacted["nested"]["CREDENTIALS"] == "[REDACTED]"
    assert redacted["account_number"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_audit_logger_contract_validation(postgres_container, override_database_url):
    """Verify that the logger enforces the presence of required event contract keys."""
    async with async_session_maker() as session:
        logger = AuditLogger(session)

        # Missing required keys (e.g., entity_public_id)
        with pytest.raises(ValueError) as exc:
            await logger.log(
                workspace_id=1,
                actor_id=1,
                action="create",
                module="todo",
                entity_type="todo",
                entity_id=1,
                details={"after": {}, "changed_fields": []},  # Missing entity_public_id, before
            )
        assert "Missing required audit detail key" in str(exc.value)


@pytest.mark.asyncio
async def test_audit_logger_action_rules(postgres_container, override_database_url):
    """Verify action-level nullability constraints."""
    async with async_session_maker() as session:
        logger = AuditLogger(session)

        entity_uuid = str(uuid.uuid4())

        # 'create' must have before=None
        with pytest.raises(ValueError) as exc:
            await logger.log(
                workspace_id=1,
                actor_id=1,
                action="create",
                module="todo",
                entity_type="todo",
                entity_id=1,
                details={
                    "entity_public_id": entity_uuid,
                    "before": {"title": "Old"},
                    "after": {"title": "New"},
                    "changed_fields": [],
                    "request_id": None,
                },
            )
        assert "Create action must have before = null" in str(exc.value)

        # 'update' must have both before and after not-null
        with pytest.raises(ValueError) as exc:
            await logger.log(
                workspace_id=1,
                actor_id=1,
                action="update",
                module="todo",
                entity_type="todo",
                entity_id=1,
                details={
                    "entity_public_id": entity_uuid,
                    "before": None,
                    "after": {"title": "New"},
                    "changed_fields": [],
                    "request_id": None,
                },
            )
        assert "update action must have both before and after != null" in str(exc.value)


@pytest.mark.asyncio
async def test_audit_log_transactional_commit(postgres_container, override_database_url):
    """Verify that audit logs are committed when the transaction succeeds."""
    async with async_session_maker() as session:
        logger = AuditLogger(session)
        entity_uuid = str(uuid.uuid4())

        await logger.log(
            workspace_id=1,
            actor_id=1,
            action="create",
            module="todo",
            entity_type="todo",
            entity_id=1,
            details={
                "entity_public_id": entity_uuid,
                "before": None,
                "after": {"title": "Test Todo"},
                "changed_fields": ["title"],
                "request_id": None,
            },
        )
        # Commit the transaction
        await session.commit()

    # Query in a new session to verify the log exists
    async with async_session_maker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.workspace_id == 1))
        logs = result.scalars().all()
        assert len(logs) == 1
        assert logs[0].action == "create"
        assert logs[0].details["after"]["title"] == "Test Todo"


@pytest.mark.asyncio
async def test_audit_log_transactional_rollback(postgres_container, override_database_url):
    """Verify that audit logs are discarded if the transaction rolls back."""
    async with async_session_maker() as session:
        logger = AuditLogger(session)
        entity_uuid = str(uuid.uuid4())

        await logger.log(
            workspace_id=1,
            actor_id=1,
            action="create",
            module="todo",
            entity_type="todo",
            entity_id=1,
            details={
                "entity_public_id": entity_uuid,
                "before": None,
                "after": {"title": "Discarded Todo"},
                "changed_fields": ["title"],
                "request_id": None,
            },
        )
        # Rollback the transaction
        await session.rollback()

    # Verify no log exists
    async with async_session_maker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.workspace_id == 1))
        logs = result.scalars().all()
        assert len(logs) == 0


@pytest.mark.asyncio
async def test_todo_service_create_audit_logging(postgres_container, override_database_url):
    """Verify that creating a todo logs a 'create' audit event."""
    async with async_session_maker() as session:
        todo_repo = TodoRepository(session)
        todo_service = TodoService(todo_repo)
        audit_logger = AuditLogger(session)

        # Create a todo
        todo_in = TodoCreate(title="Audited Todo", description="Needs audit check")
        todo = await todo_service.create_todo(
            user_id=1, workspace_id=1, todo_in=todo_in, audit_logger=audit_logger
        )
        await session.commit()

        todo_id = todo.id
        todo_uuid = todo.public_id

    # Verify the audit log was written
    async with async_session_maker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.entity_id == todo_id))
        logs = result.scalars().all()
        assert len(logs) == 1
        log = logs[0]
        assert log.action == "create"
        assert log.module == "todo"
        assert log.entity_type == "todo"
        assert log.details["entity_public_id"] == str(todo_uuid)
        assert log.details["before"] is None
        assert log.details["after"]["title"] == "Audited Todo"


@pytest.mark.asyncio
async def test_todo_service_update_audit_logging(postgres_container, override_database_url):
    """Verify that updating a todo logs an 'update' audit event."""
    async with async_session_maker() as session:
        todo_repo = TodoRepository(session)
        todo_service = TodoService(todo_repo)
        audit_logger = AuditLogger(session)

        todo_in = TodoCreate(title="Original Title", description="Before update")
        todo = await todo_service.create_todo(
            user_id=1, workspace_id=1, todo_in=todo_in, audit_logger=audit_logger
        )
        await session.commit()
        todo_id = todo.id
        todo_uuid = todo.public_id

    # Perform update in a new session
    async with async_session_maker() as session:
        todo_repo = TodoRepository(session)
        todo_service = TodoService(todo_repo)
        audit_logger = AuditLogger(session)

        todo_update = TodoUpdate(title="Updated Title")
        await todo_service.update_todo(
            workspace_id=1,
            public_id=todo_uuid,
            todo_in=todo_update,
            actor_id=1,
            audit_logger=audit_logger,
        )
        await session.commit()

    # Verify update audit log
    async with async_session_maker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.entity_id == todo_id).order_by(AuditLog.timestamp.asc())
        )
        logs = result.scalars().all()
        assert len(logs) == 2  # 1: create, 2: update
        update_log = logs[1]
        assert update_log.action == "update"
        assert update_log.details["before"]["title"] == "Original Title"
        assert update_log.details["after"]["title"] == "Updated Title"
        assert update_log.details["changed_fields"] == ["title"]


@pytest.mark.asyncio
async def test_todo_service_complete_audit_logging(postgres_container, override_database_url):
    """Verify that completing a todo logs a 'complete' audit event instead of 'update'."""
    async with async_session_maker() as session:
        todo_repo = TodoRepository(session)
        todo_service = TodoService(todo_repo)
        audit_logger = AuditLogger(session)

        todo_in = TodoCreate(title="Completable Todo")
        todo = await todo_service.create_todo(
            user_id=1, workspace_id=1, todo_in=todo_in, audit_logger=audit_logger
        )
        await session.commit()
        todo_id = todo.id
        todo_uuid = todo.public_id

    # Complete it
    async with async_session_maker() as session:
        todo_repo = TodoRepository(session)
        todo_service = TodoService(todo_repo)
        audit_logger = AuditLogger(session)

        todo_update = TodoUpdate(completed=True)
        await todo_service.update_todo(
            workspace_id=1,
            public_id=todo_uuid,
            todo_in=todo_update,
            actor_id=1,
            audit_logger=audit_logger,
        )
        await session.commit()

    # Verify complete audit log
    async with async_session_maker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.entity_id == todo_id).order_by(AuditLog.timestamp.asc())
        )
        logs = result.scalars().all()
        assert len(logs) == 2
        complete_log = logs[1]
        assert complete_log.action == "complete"
        assert complete_log.details["before"]["completed"] is False
        assert complete_log.details["after"]["completed"] is True


@pytest.mark.asyncio
async def test_todo_service_delete_audit_logging(postgres_container, override_database_url):
    """Verify that deleting a todo logs a 'delete' audit event."""
    async with async_session_maker() as session:
        todo_repo = TodoRepository(session)
        todo_service = TodoService(todo_repo)
        audit_logger = AuditLogger(session)

        todo_in = TodoCreate(title="Deletable Todo")
        todo = await todo_service.create_todo(
            user_id=1, workspace_id=1, todo_in=todo_in, audit_logger=audit_logger
        )
        await session.commit()
        todo_id = todo.id
        todo_uuid = todo.public_id

    # Delete it
    async with async_session_maker() as session:
        todo_repo = TodoRepository(session)
        todo_service = TodoService(todo_repo)
        audit_logger = AuditLogger(session)

        await todo_service.delete_todo(
            workspace_id=1, public_id=todo_uuid, actor_id=1, audit_logger=audit_logger
        )
        await session.commit()

    # Verify delete audit log
    async with async_session_maker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.entity_id == todo_id).order_by(AuditLog.timestamp.asc())
        )
        logs = result.scalars().all()
        assert len(logs) == 2
        delete_log = logs[1]
        assert delete_log.action == "delete"
        assert delete_log.details["before"]["title"] == "Deletable Todo"
        assert delete_log.details["after"] is None
