from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.auth.repository import AuthSessionRepository, UserRepository
from app.auth.schemas import UserCreate
from app.config import settings
from app.core.database import postgres
from app.core.dependencies import get_client_ip


@pytest.mark.asyncio
async def test_x_request_id_header(client: AsyncClient):
    """Verify that X-Request-ID is present in response headers."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert "X-Request-ID" in response.headers
    # Should be a UUID
    assert len(response.headers["X-Request-ID"]) == 36


@pytest.mark.asyncio
async def test_hsts_header_with_forwarded_proto(client: AsyncClient):
    """Verify that HSTS header is sent when X-Forwarded-Proto is https."""
    # CASE 1: No proto header → no HSTS
    response = await client.get("/health")
    assert "Strict-Transport-Security" not in response.headers

    # CASE 2: X-Forwarded-Proto: https → HSTS present
    response = await client.get("/health", headers={"X-Forwarded-Proto": "https"})
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"


@pytest.mark.asyncio
async def test_x_xss_protection_header(client: AsyncClient):
    """Verify that X-XSS-Protection is present."""
    response = await client.get("/health")
    assert response.headers["X-XSS-Protection"] == "1; mode=block"


@pytest.mark.asyncio
async def test_configurable_csp(client: AsyncClient):
    """Verify that CSP headers reflect settings."""
    response = await client.get("/health")
    assert "Content-Security-Policy" in response.headers
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


@pytest.mark.asyncio
async def test_inactive_user_login(client: AsyncClient):
    """Verify that an inactive user cannot log in."""
    async_session_maker = postgres.get_session_maker(postgres.engine)
    async with async_session_maker() as session:
        repo = UserRepository(session)
        user = await repo.create(
            UserCreate(
                username="inactive_user", email="inactive@example.com", password="TestPass123!"
            )
        )
        user.is_active = False
        session.add(user)
        await session.commit()

    response = await client.post(
        "/v1/auth/login", data={"username": "inactive_user", "password": "TestPass123!"}
    )
    assert response.status_code == 401
    assert "Incorrect username or password" in response.json()["detail"]


@pytest.mark.asyncio
async def test_register_enumeration_protection(client: AsyncClient):
    """Verify registration endpoint returns identical generic error on duplicate email or username."""
    # Try to register a user
    email = "enum_user@example.com"
    username = "enum_user"
    resp = await client.post(
        "/v1/auth/register",
        json={"email": email, "username": username, "password": "TestPass123!"},
    )
    assert resp.status_code == 200

    # Try to register again with same email but different username
    resp_email = await client.post(
        "/v1/auth/register",
        json={"email": email, "username": "unique_username", "password": "TestPass123!"},
    )
    assert resp_email.status_code == 409
    assert resp_email.json()["detail"] == "Registration failed. Invalid or taken username/email."

    # Try to register again with unique email but same username
    resp_user = await client.post(
        "/v1/auth/register",
        json={
            "email": "unique_email@example.com",
            "username": username,
            "password": "TestPass123!",
        },
    )
    assert resp_user.status_code == 409
    assert resp_user.json()["detail"] == "Registration failed. Invalid or taken username/email."


@pytest.mark.asyncio
async def test_refresh_token_rotation_and_reuse_detection(client: AsyncClient):
    """Verify refresh token rotation, grace period timeout retry, and replay protection."""
    # 1. Create a user first so we can login
    async_session_maker = postgres.get_session_maker(postgres.engine)
    async with async_session_maker() as session:
        repo = UserRepository(session)
        user = await repo.get_by_username("rotation_user")
        if not user:
            user = await repo.create(
                UserCreate(
                    username="rotation_user", email="rotation@example.com", password="TestPass123!"
                )
            )
            await session.commit()

    # 2. Login to get initial cookies
    login_resp = await client.post(
        "/v1/auth/login", data={"username": "rotation_user", "password": "TestPass123!"}
    )
    assert login_resp.status_code == 200

    # Extract cookies as a dictionary
    cookies_1 = dict(login_resp.cookies)
    refresh_token_1 = cookies_1.get("refresh_token")
    access_token_1 = cookies_1.get("access_token")
    sid_1 = cookies_1.get("sid")
    assert refresh_token_1 is not None
    assert access_token_1 is not None
    assert sid_1 is not None

    # 3. First refresh (valid rotation)
    refresh_resp = await client.post("/v1/auth/refresh", cookies=cookies_1)
    assert refresh_resp.status_code == 200

    cookies_2 = dict(refresh_resp.cookies)
    refresh_token_2 = cookies_2.get("refresh_token")
    access_token_2 = cookies_2.get("access_token")
    sid_2 = cookies_2.get("sid")

    assert refresh_token_2 != refresh_token_1
    assert access_token_2 != access_token_1
    assert sid_2 == sid_1  # SID remains same for session family

    # 4. Legitimate retry within 5-second grace period (reuse refresh_token_1)
    # We pass cookies_1 to simulate the retry before receiving the rotated ones
    grace_resp = await client.post("/v1/auth/refresh", cookies=cookies_1)
    assert grace_resp.status_code == 200

    cookies_2_retry = dict(grace_resp.cookies)
    refresh_token_2_retry = cookies_2_retry.get("refresh_token")
    assert refresh_token_2_retry is not None

    # 5. Attempt to reuse refresh_token_1 again but outside the grace period (we patch rotated_at to 10s ago)
    async with async_session_maker() as session:
        repo = AuthSessionRepository(session)
        auth_sess = await repo.get_active_by_sid(sid_1)
        assert auth_sess is not None
        auth_sess.rotated_at = datetime.now(UTC) - timedelta(seconds=10)
        session.add(auth_sess)
        await session.commit()

    # Now attempt refresh with the old refresh_token_1
    replay_resp = await client.post("/v1/auth/refresh", cookies=cookies_1)
    assert replay_resp.status_code == 401
    assert "Replay attack detected" in replay_resp.json()["detail"]

    # Verify cookies were cleared in response
    set_cookie_headers = replay_resp.headers.get_list("set-cookie")
    assert any("refresh_token=" in h and "max-age=0" in h.lower() for h in set_cookie_headers)
    assert any("access_token=" in h and "max-age=0" in h.lower() for h in set_cookie_headers)
    assert any("sid=" in h and "max-age=0" in h.lower() for h in set_cookie_headers)

    # Verify session is now revoked in database
    async with async_session_maker() as session:
        repo = AuthSessionRepository(session)
        auth_sess_check = await repo.get_active_by_sid(sid_1)
        assert auth_sess_check is None


@pytest.mark.asyncio
async def test_get_client_ip_proxy_validation():
    """Verify that get_client_ip correctly validates X-Forwarded-For based on TRUSTED_PROXIES."""

    # Helper mock connection info
    class MockClient:
        def __init__(self, host: str):
            self.host = host

    class MockRequest:
        def __init__(self, client_host: str | None, headers: dict):
            self.client = MockClient(client_host) if client_host else None
            self.headers = headers

    # Make sure settings has expected TRUSTED_PROXIES
    settings.TRUSTED_PROXIES = ["127.0.0.1", "1.2.3.4"]

    # CASE 1: Untrusted client IP with X-Forwarded-For -> must ignore header and return client IP
    req_untrusted = MockRequest(
        client_host="5.5.5.5", headers={"x-forwarded-for": "9.9.9.9, 8.8.8.8"}
    )
    assert get_client_ip(req_untrusted) == "5.5.5.5"

    # CASE 2: Trusted client IP with X-Forwarded-For -> must trust header and return leftmost IP
    req_trusted = MockRequest(
        client_host="127.0.0.1", headers={"x-forwarded-for": "9.9.9.9, 8.8.8.8"}
    )
    assert get_client_ip(req_trusted) == "9.9.9.9"

    # CASE 3: Trusted client IP with no X-Forwarded-For -> must return client IP
    req_trusted_no_header = MockRequest(client_host="127.0.0.1", headers={})
    assert get_client_ip(req_trusted_no_header) == "127.0.0.1"

    # CASE 4: No client connection info -> must return "unknown"
    req_no_client = MockRequest(client_host=None, headers={"x-forwarded-for": "9.9.9.9"})
    assert get_client_ip(req_no_client) == "unknown"
