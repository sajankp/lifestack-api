import pytest
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from app.config import settings
from app.main import app


@pytest.fixture(scope="session")
def postgres_container():
    """Start the Postgres testcontainer and expose it to the test session."""
    with PostgresContainer("postgres:15-alpine") as postgres:
        yield postgres


@pytest.fixture(scope="session")
def override_database_url(postgres_container):
    """Override the database URL in the settings to point at the testcontainer."""
    url = postgres_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql+asyncpg://"
    )
    settings.DATABASE_URL = url
    return url


@pytest.fixture
async def client(override_database_url):
    """Return an AsyncClient that hits the app."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
