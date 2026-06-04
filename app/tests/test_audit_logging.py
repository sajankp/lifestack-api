"""
Unit tests for AuditLogger mechanics.

These tests cover internal behaviour that is difficult or inappropriate to
exercise through the HTTP E2E tests:
  - Redaction of sensitive keys in the JSONB payload
  - Contract validation (required keys, action-level null rules)
  - Transactional atomicity (commit persists, rollback discards)

Service-level integration tests (e.g. TodoService calling AuditLogger) are
intentionally omitted here — they are fully covered by the E2E audit tests in
  app/tests/routers/test_todo_audit.py
  app/tests/integration/test_spending_audit.py
which additionally co-verify that the audit log faithfully mirrors the actual
DB entity state after each mutation.
"""

import uuid

import pytest
from sqlalchemy import select

from app.auth.models import User
from app.core.audit import AuditLog, AuditLogger, redact_details
from app.core.database import postgres
from app.platform.models import Workspace


@pytest.fixture(autouse=True)
async def seed_audit_test_data(override_database_url):
    """Seed the user and workspaces needed for audit logging foreign key constraints.

    Workspace IDs used by remaining tests:
      901 — test_audit_logger_contract_validation
      902 — test_audit_logger_action_rules
      903 — test_audit_log_transactional_commit
      904 — test_audit_log_transactional_rollback
    """
    async with postgres.async_session_maker() as session:
        user = User(
            id=1,
            email="audit_actor@example.com",
            username="audit_actor",
            hashed_password="hashed_password_here",
        )
        session.add(user)
        for wid in range(901, 905):  # 901, 902, 903, 904
            ws = Workspace(id=wid, name=f"Workspace {wid}")
            session.add(ws)
        await session.commit()


@pytest.mark.asyncio
async def test_redact_details_sensitive_keys():
    """Verify that sensitive keys are recursively redacted case-insensitively."""
    raw_payload = {
        "user_id": 42,
        "password": "TestPass123!",
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
    async with postgres.async_session_maker() as session:
        logger = AuditLogger(session)

        # Missing required keys (e.g., entity_public_id)
        with pytest.raises(ValueError) as exc:
            await logger.log(
                workspace_id=901,
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
    async with postgres.async_session_maker() as session:
        logger = AuditLogger(session)

        entity_uuid = str(uuid.uuid4())

        # 'create' must have before=None
        with pytest.raises(ValueError) as exc:
            await logger.log(
                workspace_id=902,
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
                workspace_id=902,
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
    async with postgres.async_session_maker() as session:
        logger = AuditLogger(session)
        entity_uuid = str(uuid.uuid4())

        await logger.log(
            workspace_id=903,
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
    async with postgres.async_session_maker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.workspace_id == 903))
        logs = result.scalars().all()
        assert len(logs) == 1
        assert logs[0].action == "create"
        assert logs[0].details["after"]["title"] == "Test Todo"


@pytest.mark.asyncio
async def test_audit_log_transactional_rollback(postgres_container, override_database_url):
    """Verify that audit logs are discarded if the transaction rolls back."""
    async with postgres.async_session_maker() as session:
        logger = AuditLogger(session)
        entity_uuid = str(uuid.uuid4())

        await logger.log(
            workspace_id=904,
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
    async with postgres.async_session_maker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.workspace_id == 904))
        logs = result.scalars().all()
        assert len(logs) == 0
