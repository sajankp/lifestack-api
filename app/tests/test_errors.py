import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.core.exceptions import APIError
from app.main import app

# Create some helper endpoints to trigger errors
error_router = APIRouter()


class Item(BaseModel):
    name: str


@error_router.get("/test-404")
async def trigger_404():
    raise APIError(
        type_str="not-found",
        title="Resource Not Found",
        status_code=404,
        detail="The requested resource could not be found.",
    )


@error_router.post("/test-422")
async def trigger_422(item: Item):
    return item


@error_router.get("/test-500")
async def trigger_500():
    raise ValueError("Something went terribly wrong!")


app.include_router(error_router)


@pytest.mark.asyncio
async def test_api_error_returns_rfc7807():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/test-404")
        assert response.status_code == 404
        data = response.json()
        assert data["type"] == "https://lifestack.app/errors/not-found"
        assert data["title"] == "Resource Not Found"
        assert data["status"] == 404
        assert data["detail"] == "The requested resource could not be found."
        assert data["instance"] == "/test-404"


@pytest.mark.asyncio
async def test_validation_error_returns_rfc7807():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/test-422", json={"wrong_field": "value"})
        assert response.status_code == 422
        data = response.json()
        assert data["type"] == "https://lifestack.app/errors/validation-error"
        assert data["title"] == "Request Validation Error"
        assert data["status"] == 422
        assert "detail" in data
        assert data["instance"] == "/test-422"
        assert "errors" in data
        assert len(data["errors"]) > 0


@pytest.mark.asyncio
async def test_unhandled_exception_returns_rfc7807():
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get("/test-500")
        assert response.status_code == 500
        data = response.json()
        assert data["type"] == "https://lifestack.app/errors/internal-server-error"
        assert data["title"] == "Internal Server Error"
        assert data["status"] == 500
        assert data["instance"] == "/test-500"
        assert "detail" in data
