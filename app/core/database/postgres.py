from collections.abc import AsyncGenerator

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

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


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for injecting database sessions."""
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception as e:
            logger.error("db_session_rollback", exc_info=True)
            await session.rollback()
            raise e
        finally:
            await session.close()
