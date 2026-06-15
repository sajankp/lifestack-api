from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.auth.models import AuthSession, PasswordResetToken, User
from app.core.database import postgres


@pytest.mark.asyncio
async def test_forgot_password_generic_success(client: AsyncClient):
    # Test with email that exists
    user_data = {
        "email": "exists@example.com",
        "username": "existsuser",
        "password": "TestPass123!",
    }
    register_res = await client.post("/v1/auth/register", json=user_data)
    assert register_res.status_code == 200

    forgot_res = await client.post("/v1/auth/forgot-password", json={"email": "exists@example.com"})
    assert forgot_res.status_code == 200
    assert (
        forgot_res.json()["message"]
        == "If the email is registered, a password reset link has been sent."
    )

    # Test with email that does NOT exist (should return identical response to prevent enumeration)
    forgot_res_nonexistent = await client.post(
        "/v1/auth/forgot-password", json={"email": "nonexistent@example.com"}
    )
    assert forgot_res_nonexistent.status_code == 200
    assert (
        forgot_res_nonexistent.json()["message"]
        == "If the email is registered, a password reset link has been sent."
    )


@pytest.mark.asyncio
async def test_password_reset_flow_with_mocked_token(client: AsyncClient, monkeypatch):
    test_token = "mocked-reset-token-12345"
    monkeypatch.setattr("secrets.token_urlsafe", lambda *args, **kwargs: test_token)

    # 1. Register a user
    user_data = {
        "email": "test-reset@example.com",
        "username": "testresetuser",
        "password": "TestPass123!",
    }
    register_res = await client.post("/v1/auth/register", json=user_data)
    assert register_res.status_code == 200

    # 2. Trigger forgot password
    forgot_res = await client.post(
        "/v1/auth/forgot-password", json={"email": "test-reset@example.com"}
    )
    assert forgot_res.status_code == 200

    # 3. Use the token to reset the password
    reset_res = await client.post(
        "/v1/auth/reset-password",
        json={"token": test_token, "new_password": "NewSecurePassword456!"},
    )
    assert reset_res.status_code == 200
    assert reset_res.json()["message"] == "Password has been reset successfully."

    # 4. Verify logging in with old password fails
    login_old_res = await client.post(
        "/v1/auth/login", data={"username": "testresetuser", "password": "TestPass123!"}
    )
    assert login_old_res.status_code == 401

    # 5. Verify logging in with new password succeeds
    login_new_res = await client.post(
        "/v1/auth/login", data={"username": "testresetuser", "password": "NewSecurePassword456!"}
    )
    assert login_new_res.status_code == 200


@pytest.mark.asyncio
async def test_password_reset_fails_expired(client: AsyncClient, monkeypatch):
    test_token = "expired-token"
    monkeypatch.setattr("secrets.token_urlsafe", lambda *args, **kwargs: test_token)

    # 1. Register a user
    user_data = {
        "email": "expired@example.com",
        "username": "expireduser",
        "password": "TestPass123!",
    }
    await client.post("/v1/auth/register", json=user_data)

    # 2. Trigger forgot password
    await client.post("/v1/auth/forgot-password", json={"email": "expired@example.com"})

    # 3. Set expires_at in the past
    async with postgres.async_session_maker() as session:
        statement = select(PasswordResetToken)
        result = await session.execute(statement)
        token_record = result.scalars().first()
        token_record.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        session.add(token_record)
        await session.commit()

    # 4. Attempt reset and expect 400 Bad Request
    reset_res = await client.post(
        "/v1/auth/reset-password",
        json={"token": test_token, "new_password": "NewSecurePassword456!"},
    )
    assert reset_res.status_code == 400
    assert "Invalid or expired" in reset_res.json()["detail"]


@pytest.mark.asyncio
async def test_password_reset_fails_already_used(client: AsyncClient, monkeypatch):
    test_token = "used-token"
    monkeypatch.setattr("secrets.token_urlsafe", lambda *args, **kwargs: test_token)

    # 1. Register a user
    user_data = {
        "email": "used@example.com",
        "username": "useduser",
        "password": "TestPass123!",
    }
    await client.post("/v1/auth/register", json=user_data)

    # 2. Trigger forgot password
    await client.post("/v1/auth/forgot-password", json={"email": "used@example.com"})

    # 3. Set used_at in the database
    async with postgres.async_session_maker() as session:
        statement = select(PasswordResetToken)
        result = await session.execute(statement)
        token_record = result.scalars().first()
        token_record.used_at = datetime.now(UTC)
        session.add(token_record)
        await session.commit()

    # 4. Attempt reset and expect 400 Bad Request
    reset_res = await client.post(
        "/v1/auth/reset-password",
        json={"token": test_token, "new_password": "NewSecurePassword456!"},
    )
    assert reset_res.status_code == 400
    assert "Invalid or expired" in reset_res.json()["detail"]


@pytest.mark.asyncio
async def test_password_reset_invalidates_all_sessions(client: AsyncClient, monkeypatch):
    test_token = "session-invalidation-token"
    monkeypatch.setattr("secrets.token_urlsafe", lambda *args, **kwargs: test_token)

    # 1. Register a user
    user_data = {
        "email": "session-inv@example.com",
        "username": "sessioninvuser",
        "password": "TestPass123!",
    }
    await client.post("/v1/auth/register", json=user_data)

    # 2. Log in (creates a session)
    login_res = await client.post(
        "/v1/auth/login", data={"username": "sessioninvuser", "password": "TestPass123!"}
    )
    assert login_res.status_code == 200

    # 3. Verify active sessions in DB
    async with postgres.async_session_maker() as session:
        statement = select(AuthSession).where(AuthSession.revoked_at.is_(None))
        result = await session.execute(statement)
        active_sessions = result.scalars().all()
        assert len(active_sessions) > 0

    # 4. Trigger forgot password and reset it
    await client.post("/v1/auth/forgot-password", json={"email": "session-inv@example.com"})
    reset_res = await client.post(
        "/v1/auth/reset-password",
        json={"token": test_token, "new_password": "NewSecurePassword456!"},
    )
    assert reset_res.status_code == 200

    # 5. Verify sessions are now revoked
    async with postgres.async_session_maker() as session:
        user_statement = select(User).where(User.email == "session-inv@example.com")
        user_result = await session.execute(user_statement)
        user_record = user_result.scalar_one()
        user_id = user_record.id

        statement = select(AuthSession).where(
            AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
        )
        result = await session.execute(statement)
        active_sessions_after = result.scalars().all()
        assert len(active_sessions_after) == 0
