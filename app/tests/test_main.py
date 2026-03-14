import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app


@pytest.mark.asyncio
async def test_health_check():
    """Verify that the health check endpoint returns 200 OK."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "version": settings.VERSION}


@pytest.mark.asyncio
async def test_docs_reachable():
    """Verify that the OpenAPI docs are reachable."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/docs")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_versioned_openapi_reachable():
    """Verify that the versioned OpenAPI schema stays publicly reachable."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"{settings.API_V1_STR}/openapi.json")
        assert response.status_code == 200
