from http.cookies import SimpleCookie

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from limits.storage import storage_from_string
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from alembic import command
from app.config import settings
from app.core import dependencies as core_dependencies
from app.core.cache import ResponseCache
from app.core.database import postgres
from app.core.dependencies import limiter
from app.main import app

_cached_tables: str | None = None


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
    settings.RUN_BACKGROUND_TASKS_SYNCHRONOUSLY = True

    strategy_cls = limiter._limiter.__class__
    limiter._storage = storage_from_string(url)
    limiter._limiter = strategy_cls(limiter._storage)
    yield url


@pytest.fixture
def enable_response_cache(redis_container):
    """Point the response cache singleton at the Redis testcontainer and enable it for one test.

    ENABLE_RESPONSE_CACHE defaults False so tests get live behavior unless they opt in via this
    fixture (spec-087 testing plan). Uses DB 2 to avoid any key overlap with rate-limit storage.
    """
    url = f"redis://{redis_container.get_container_host_ip()}:{redis_container.get_exposed_port(6379)}/2"
    original = core_dependencies.response_cache
    core_dependencies.response_cache = ResponseCache(url, enabled=True)
    yield core_dependencies.response_cache
    core_dependencies.response_cache = original


@pytest.fixture(scope="session")
def migrated_database_url(postgres_container):
    """Create the migrated test database once for the test session."""
    url = postgres_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql+asyncpg://"
    )
    settings.DATABASE_URL = url

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.sync_database_url)
    command.upgrade(config, "head")

    return url


@pytest.fixture(scope="session")
async def test_database_engine(migrated_database_url):
    """Share one migrated test database engine across DB-backed tests."""
    engine = create_async_engine(
        migrated_database_url,
        echo=settings.LOG_LEVEL == "DEBUG",
        future=True,
        poolclass=NullPool,
    )
    yield engine
    await engine.dispose()


@pytest.fixture
async def override_database_url(migrated_database_url, test_database_engine):
    """Point the app at a clean migrated test database for one test run using transactional rollbacks."""
    settings.DATABASE_URL = migrated_database_url
    postgres.engine = test_database_engine

    connection = await test_database_engine.connect()
    transaction = await connection.begin()

    session_maker = async_sessionmaker(
        bind=connection,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    postgres.async_session_maker = session_maker

    original_get_session_maker = postgres.get_session_maker
    postgres.get_session_maker = lambda engine: session_maker

    yield migrated_database_url

    postgres.get_session_maker = original_get_session_maker
    await transaction.rollback()
    await connection.close()


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

    async def add_csrf_header(request):
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        if "x-csrf-token" in request.headers:
            return

        cookie_header = request.headers.get("cookie")
        if not cookie_header:
            return

        cookie = SimpleCookie()
        cookie.load(cookie_header)
        csrf_token = cookie.get("csrf_token")
        if csrf_token:
            request.headers["X-CSRF-Token"] = csrf_token.value

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 123), raise_app_exceptions=False),
        base_url="http://test",
        headers={"Origin": "http://test"},
        event_hooks={"request": [add_csrf_header]},
    ) as ac:
        yield ac
    limiter.enabled = True
