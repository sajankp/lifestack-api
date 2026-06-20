from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.database.postgres import get_db_session
from app.core.exceptions import UnauthorizedError


@pytest.mark.asyncio
async def test_expected_api_error_rolls_back_without_error_traceback():
    session = AsyncMock()
    session_context = AsyncMock()
    session_context.__aenter__.return_value = session
    session_context.__aexit__.return_value = None

    with (
        patch("app.core.database.postgres.async_session_maker", return_value=session_context),
        patch("app.core.database.postgres.logger") as logger,
    ):
        dependency = get_db_session()
        await dependency.__anext__()

        with pytest.raises(UnauthorizedError):
            await dependency.athrow(UnauthorizedError(detail="Not authenticated"))

    session.rollback.assert_awaited_once()
    logger.debug.assert_called_once_with(
        "db_session_rollback_expected",
        exception_type="UnauthorizedError",
        status_code=401,
    )
    logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_unexpected_error_rolls_back_with_error_traceback():
    session = AsyncMock()
    session_context = AsyncMock()
    session_context.__aenter__.return_value = session
    session_context.__aexit__.return_value = None
    failure = RuntimeError("database failed")

    with (
        patch("app.core.database.postgres.async_session_maker", return_value=session_context),
        patch("app.core.database.postgres.logger", MagicMock()) as logger,
    ):
        dependency = get_db_session()
        await dependency.__anext__()

        with pytest.raises(RuntimeError):
            await dependency.athrow(failure)

    session.rollback.assert_awaited_once()
    logger.error.assert_called_once_with("db_session_rollback", exc_info=True)
