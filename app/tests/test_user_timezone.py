import asyncio
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfoNotFoundError

import pytest
from pydantic import ValidationError

from app.auth.models import User
from app.auth.schemas import UserTimezoneUpdate
from app.auth.service import AuthService


def test_user_timezone_accepts_iana_name():
    payload = UserTimezoneUpdate(timezone="Asia/Kolkata")
    assert payload.timezone == "Asia/Kolkata"


def test_user_timezone_can_be_cleared_for_browser_fallback():
    payload = UserTimezoneUpdate(timezone=None)
    assert payload.timezone is None


def test_user_timezone_rejects_unknown_name():
    with pytest.raises((ValidationError, ZoneInfoNotFoundError)):
        UserTimezoneUpdate(timezone="Mars/Olympus_Mons")


def test_auth_service_persists_user_timezone():
    user = User(
        id=7,
        email="user@example.com",
        username="user",
        hashed_password="hash",
    )
    user_repo = AsyncMock()
    user_repo.get_by_id.return_value = user
    user_repo.save.side_effect = lambda value: value
    service = AuthService(user_repo, AsyncMock(), reset_token_repo=AsyncMock())

    result = asyncio.run(service.update_user_timezone(7, "Asia/Kolkata"))

    assert result.timezone == "Asia/Kolkata"
    user_repo.save.assert_awaited_once_with(user)
