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


@pytest.fixture(scope="session")
def migrated_database_url(postgres_container):
    """Create the migrated test database once for the test session."""
    url = postgres_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql+asyncpg://"
    )
    settings.DATABASE_URL = url

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    command.upgrade(config, "head")

    return url


@pytest.fixture(scope="session")
async def test_database_engine(migrated_database_url):
    """Share one migrated test database engine across DB-backed tests."""
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=settings.LOG_LEVEL == "DEBUG",
        future=True,
        poolclass=NullPool,
    )
    yield engine
    await engine.dispose()


async def _reset_database(engine) -> None:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                """
                SELECT string_agg(format('%I.%I', table_schema, table_name), ', ')
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                  AND table_name NOT IN ('alembic_version', 'currencies')
                """
            )
        )
        tables = result.scalar_one()
        if tables:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def override_database_url(migrated_database_url, test_database_engine):
    """Point the app at a clean migrated test database for one test run."""
    settings.DATABASE_URL = migrated_database_url
    postgres.engine = test_database_engine
    postgres.async_session_maker = postgres.get_session_maker(postgres.engine)

    await _reset_database(postgres.engine)

    yield migrated_database_url


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
