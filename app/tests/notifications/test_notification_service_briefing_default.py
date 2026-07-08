from unittest.mock import AsyncMock, MagicMock

import pytest

from app.notifications.service import NotificationService


@pytest.fixture
def mock_repo():
    repo = AsyncMock()
    repo.create_notification.return_value = MagicMock(id=1)
    return repo


@pytest.fixture
def service(mock_repo):
    return NotificationService(mock_repo)


@pytest.mark.asyncio
async def test_briefing_defaults_to_push_on_when_subscribed_and_no_preference_row(
    service, mock_repo
):
    """spec-067 owner decision: absence of a 'briefing' preference row counts
    as enabled when the user has an active push subscription."""
    mock_repo.get_preference.return_value = None
    mock_repo.has_active_push_subscription.return_value = True

    await service.notify(
        workspace_id=1, user_id=1, category="briefing", severity="info", title="Morning briefing"
    )

    mock_repo.create_pending_push_delivery.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_briefing_stays_off_when_no_preference_row_and_no_subscription(service, mock_repo):
    mock_repo.get_preference.return_value = None
    mock_repo.has_active_push_subscription.return_value = False

    await service.notify(
        workspace_id=1, user_id=1, category="briefing", severity="info", title="Morning briefing"
    )

    mock_repo.create_pending_push_delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_briefing_explicit_muted_preference_wins_over_subscription(service, mock_repo):
    """An explicit preference row always wins over the subscription-based default."""
    mock_repo.get_preference.return_value = MagicMock(is_muted=True, channel_push=True)
    mock_repo.has_active_push_subscription.return_value = True

    result = await service.notify(
        workspace_id=1, user_id=1, category="briefing", severity="info", title="Morning briefing"
    )

    assert result is None
    mock_repo.create_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_briefing_explicit_push_off_preference_wins_over_subscription(service, mock_repo):
    mock_repo.get_preference.return_value = MagicMock(is_muted=False, channel_push=False)
    mock_repo.has_active_push_subscription.return_value = True

    await service.notify(
        workspace_id=1, user_id=1, category="briefing", severity="info", title="Morning briefing"
    )

    mock_repo.create_pending_push_delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_categories_unaffected_by_briefing_default(service, mock_repo):
    """Every non-'briefing' category keeps the existing opt-in-only behavior:
    no preference row means no push, regardless of subscription status."""
    mock_repo.get_preference.return_value = None
    mock_repo.has_active_push_subscription.return_value = True

    await service.notify(
        workspace_id=1, user_id=1, category="todo_reminder", severity="info", title="Reminder"
    )

    mock_repo.create_pending_push_delivery.assert_not_awaited()
