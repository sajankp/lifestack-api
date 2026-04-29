import anyio
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer

from alembic import command
from alembic.config import Config
from app.config import settings
from app.core.database import postgres
from app.core.dependencies import limiter
from app.main import app


@pytest.fixture(scope="session")
def postgres_container():
    """Start the Postgres testcontainer and expose it to the test session."""
    with PostgresContainer("postgres:15-alpine") as postgres:
        yield postgres


@pytest.fixture
async def override_database_url(postgres_container):
    """Point the app database settings at the testcontainer for one test run."""
    url = postgres_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql+asyncpg://"
    )
    settings.DATABASE_URL = url

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    await anyio.to_thread.run_sync(command.upgrade, config, "head")

    postgres.engine = create_async_engine(
        settings.DATABASE_URL,
        echo=settings.LOG_LEVEL == "DEBUG",
        future=True,
        poolclass=NullPool,
    )
    postgres.async_session_maker = postgres.get_session_maker(postgres.engine)

    yield url

    async with postgres.engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await postgres.engine.dispose()


@pytest.fixture
async def client(override_database_url):
    """Return an AsyncClient that hits the app."""
    # Disable rate limiting in tests to avoid Redis dependency.
    limiter.enabled = False
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    limiter.enabled = True
