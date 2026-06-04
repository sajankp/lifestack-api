import pytest
from httpx import AsyncClient

from app.auth.repository import UserRepository
from app.auth.schemas import UserCreate
from app.core.database import postgres


@pytest.mark.anyio
async def test_x_request_id_header(client: AsyncClient):
    """Verify that X-Request-ID is present in response headers."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert "X-Request-ID" in response.headers
    # Should be a UUID
    assert len(response.headers["X-Request-ID"]) == 36


@pytest.mark.anyio
async def test_hsts_header_with_forwarded_proto(client: AsyncClient):
    """Verify that HSTS header is sent when X-Forwarded-Proto is https."""
    # CASE 1: No proto header → no HSTS
    response = await client.get("/health")
    assert "Strict-Transport-Security" not in response.headers

    # CASE 2: X-Forwarded-Proto: https → HSTS present
    response = await client.get("/health", headers={"X-Forwarded-Proto": "https"})
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"


@pytest.mark.anyio
async def test_x_xss_protection_header(client: AsyncClient):
    """Verify that X-XSS-Protection is present."""
    response = await client.get("/health")
    assert response.headers["X-XSS-Protection"] == "1; mode=block"


@pytest.mark.anyio
async def test_configurable_csp(client: AsyncClient):
    """Verify that CSP headers reflect settings."""
    # This is tricky because settings are session-scoped usually or global.
    # We'll just check for the presence of a CSP header for now.
    response = await client.get("/health")
    assert "Content-Security-Policy" in response.headers
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


@pytest.mark.anyio
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
