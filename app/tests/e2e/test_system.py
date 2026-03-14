import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_check(client: AsyncClient):
    """Verify that the health check endpoint returns ok."""
    response = await client.get("/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_auth_scaffold(client: AsyncClient):
    """Verify that the auth login scaffold returns the expected response."""
    response = await client.post("/v1/auth/login")
    assert response.status_code == 200
    assert response.json() == {"message": "login endpoint scaffold"}


@pytest.mark.asyncio
async def test_todo_scaffold(client: AsyncClient):
    """Verify that the todo scaffold returns the expected response."""
    response = await client.get("/v1/todo/")
    assert response.status_code == 200
    assert response.json() == {"message": "list todos scaffold"}
