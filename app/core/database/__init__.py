# Export the postgres module and its public attributes
from app.core.database import postgres as _postgres_module
from app.core.database.postgres import (
    async_session_maker,
    engine,
    get_db_session,
    get_db_session_readonly,
    get_db_session_readwrite,
)

# Re-export the postgres module itself for code that does `from app.core.database import postgres`
postgres = _postgres_module

__all__ = [
    "engine",
    "async_session_maker",
    "get_db_session",
    "get_db_session_readonly",
    "get_db_session_readwrite",
    "postgres",
]
