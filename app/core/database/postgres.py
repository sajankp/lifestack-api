import os
from collections.abc import AsyncGenerator

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.core.exceptions import APIError

logger = structlog.get_logger()


# We will use a function to get the engine to allow for dynamic overrides during testing
def get_engine():
    # Calculate default pool sizes based on CPU count if not explicitly configured
    cpu_count = os.cpu_count() or 4
    pool_size = settings.DATABASE_POOL_SIZE if settings.DATABASE_POOL_SIZE > 0 else cpu_count * 2
    max_overflow = (
        settings.DATABASE_MAX_OVERFLOW if settings.DATABASE_MAX_OVERFLOW > 0 else pool_size
    )

    return create_async_engine(
        settings.DATABASE_URL,
        echo=settings.LOG_LEVEL == "DEBUG",
        future=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        connect_args={
            "prepared_statement_cache_size": 1000,
            "statement_cache_size": 1000,
        },
    )


def get_session_maker(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


# Default engine and session maker
engine = get_engine()
async_session_maker = get_session_maker(engine)


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    """Dependency for injecting database sessions (read-write).

    **Session Lifecycle (Request-Scoped)**

    FastAPI caches ``Depends()`` results per-request. Because every repository
    and service dependency resolves through ``Depends(get_db_session)``, all
    components within a single request share the **same** ``AsyncSession``
    instance. This gives us implicit request-scoped transactions:

    - Repositories call ``flush()`` (never ``commit()``) to surface constraint
      violations early while keeping the transaction open.
    - This generator calls ``commit()`` once, after the route handler returns
      successfully.
    - On any exception the session is rolled back, ensuring atomicity for
      multi-step workflows like user registration.

    **Implication for workflows:** ``UserRegistrationWorkflow`` (which calls
    ``AuthService``, ``WorkspaceService``, and ``CategoryService`` sequentially)
    is fully atomic — all three operate on the same session and transaction.
    """
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception as e:
            await session.rollback()
            if isinstance(e, APIError) and e.status_code < 500:
                logger.debug(
                    "db_session_rollback_expected",
                    exception_type=type(e).__name__,
                    status_code=e.status_code,
                )
            else:
                logger.error("db_session_rollback", exc_info=True)
            raise
        finally:
            await session.close()


async def get_db_session_readonly() -> AsyncGenerator[AsyncSession]:
    """Dependency for injecting read-only database sessions.

    Use this for read-heavy endpoints (list, get, search) where no writes occur.
    The session is rolled back (not committed) to avoid unnecessary transaction
    overhead and to ensure we don't accidentally hold locks or generate WAL.
    """
    async with async_session_maker() as session:
        try:
            yield session
        except Exception as e:
            await session.rollback()
            if isinstance(e, APIError) and e.status_code < 500:
                logger.debug(
                    "db_session_readonly_rollback_expected",
                    exception_type=type(e).__name__,
                    status_code=e.status_code,
                )
            else:
                logger.error("db_session_readonly_rollback", exc_info=True)
            raise
        finally:
            await session.close()


async def get_db_session_readwrite() -> AsyncGenerator[AsyncSession]:
    """Dependency for injecting read-write database sessions.

    Alias for ``get_db_session`` for explicit intent. Use this for endpoints
    that perform writes (POST, PATCH, DELETE).
    """
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception as e:
            await session.rollback()
            if isinstance(e, APIError) and e.status_code < 500:
                logger.debug(
                    "db_session_readwrite_rollback_expected",
                    exception_type=type(e).__name__,
                    status_code=e.status_code,
                )
            else:
                logger.error("db_session_readwrite_rollback", exc_info=True)
            raise
        finally:
            await session.close()
