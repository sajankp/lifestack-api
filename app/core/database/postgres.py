from collections.abc import AsyncGenerator

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.core.exceptions import APIError

logger = structlog.get_logger()


# We will use a function to get the engine to allow for dynamic overrides during testing
def get_engine():
    return create_async_engine(
        settings.DATABASE_URL,
        echo=settings.LOG_LEVEL == "DEBUG",
        future=True,
        pool_size=5,
        max_overflow=10,
    )


def get_session_maker(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


# Default engine and session maker
engine = get_engine()
async_session_maker = get_session_maker(engine)


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    """Dependency for injecting database sessions.

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
