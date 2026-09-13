import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.jobs import monthly_summary_job
from app.platform.models import WorkspaceRole


@pytest.fixture(scope="session", autouse=True)
def override_redis_url():
    yield None


@pytest.mark.asyncio
async def test_monthly_summary_job_generates_and_notifies():
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__.return_value = None

    mock_begin = AsyncMock()
    mock_begin.__aenter__.return_value = None
    mock_begin.__aexit__.return_value = None
    mock_session.begin = MagicMock(return_value=mock_begin)

    # Advisory lock acquired
    mock_lock_res = MagicMock()
    mock_lock_res.scalar.return_value = True

    # Active workspace ids
    mock_ws_res = MagicMock()
    mock_ws_res.scalars.return_value.all.return_value = [1]

    # Workspace memberships (2 users: 1 owner, 1 member)
    mock_memberships = MagicMock()
    mock_memberships.all.return_value = [
        (1, 101, WorkspaceRole.OWNER),
        (1, 102, WorkspaceRole.MEMBER),
    ]

    mock_session.execute.side_effect = [
        mock_lock_res,
        mock_ws_res,
        mock_memberships,
    ]

    mock_summary = MagicMock()
    mock_summary.public_id = uuid.uuid4()

    with (
        patch("app.core.database.postgres.async_session_maker", return_value=mock_session),
        patch("app.application.jobs.MonthlySummaryService") as mock_service_cls,
        patch("app.application.jobs.NotificationService") as mock_notif_cls,
    ):
        mock_service = mock_service_cls.return_value
        mock_service.generate_for_workspace_month = AsyncMock(return_value=mock_summary)
        mock_notif = mock_notif_cls.return_value
        mock_notif.notify = AsyncMock()

        target_month = date(2026, 8, 1)
        await monthly_summary_job(workspace_id=1, month_start=target_month)

        # Verified service called with primary user
        mock_service.generate_for_workspace_month.assert_awaited_once_with(1, 101, target_month)

        # Verified second user receives member notification
        mock_notif.notify.assert_awaited_once()
        call_kwargs = mock_notif.notify.call_args.kwargs
        assert call_kwargs["workspace_id"] == 1
        assert call_kwargs["user_id"] == 102
        assert "August 2026" in call_kwargs["title"]
