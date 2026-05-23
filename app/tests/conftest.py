import anyio
import pytest
from httpx import ASGITransport, AsyncClient
from limits.storage import storage_from_string
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from alembic import command
from alembic.config import Config
from app.config import settings
from app.core.database import postgres
from app.core.dependencies import limiter
from app.main import app


@pytest.fixture(scope="session")
def postgres_container():
    """Start the Postgres testcontainer and expose it to the test session."""
    with PostgresContainer("postgres:15-alpine") as postgres_c:
        yield postgres_c


@pytest.fixture(scope="session")
def redis_container():
    """Start the Redis testcontainer and expose it to the test session."""
    with RedisContainer("redis:7-alpine") as redis_c:
        yield redis_c


@pytest.fixture(scope="session", autouse=True)
def override_redis_url(redis_container):
    """Point the app rate limit settings at the Redis testcontainer and re-init storage."""
    url = f"redis://{redis_container.get_container_host_ip()}:{redis_container.get_exposed_port(6379)}"
    settings.RATE_LIMIT_STORAGE_URI = url

    strategy_cls = limiter._limiter.__class__
    limiter._storage = storage_from_string(url)
    limiter._limiter = strategy_cls(limiter._storage)
    yield url


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

    # Ensure http://test is trusted for CSRF during tests.
    # We use a set/list check and update the setting directly.
    current_trusted = settings.csrf_trusted_origins
    if "http://test" not in current_trusted:
        settings.CSRF_TRUSTED_ORIGINS = ["http://test"]

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 123), raise_app_exceptions=False),
        base_url="http://test",
        headers={"Origin": "http://test"},
    ) as ac:
        yield ac
    limiter.enabled = True
