import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_check(client: AsyncClient):
    """Verify that the health check endpoint returns ok."""
    response = await client.get("/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_auth_registration_and_login(client: AsyncClient):
    """Verify that we can register and login."""
    # Register
    register_response = await client.post(
        "/v1/auth/register",
        json={"email": "test@example.com", "username": "testuser", "password": "testpassword"},
    )
    assert register_response.status_code == 200
    assert register_response.json() is True

    # Login
    login_response = await client.post(
        "/v1/auth/login", data={"username": "testuser", "password": "testpassword"}
    )
    assert login_response.status_code == 200
    assert "access_token" in login_response.cookies
    assert "refresh_token" in login_response.cookies


@pytest.mark.asyncio
async def test_todo_protected(client: AsyncClient):
    """Verify that the todo endpoint is protected."""
    response = await client.get("/v1/todo/")
    assert response.status_code == 401
    assert response.json()["title"] == "Unauthorized"
