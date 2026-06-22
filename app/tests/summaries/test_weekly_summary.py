from datetime import date, timedelta

import pytest
from httpx import AsyncClient

from app.application.jobs import weekly_summary_job
from app.auth.repository import UserRepository
from app.core.database import postgres
from app.investing.models import PortfolioSnapshot
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService
from app.platform.repository import WorkspaceRepository
from app.summaries.repository import WeeklySummaryRepository
from app.summaries.service import WeeklySummaryService
from app.tests.integration.test_spending import _register_and_login


@pytest.mark.asyncio
async def test_weekly_summary_service_endpoints_and_job(client: AsyncClient):
    # Register and log in user
    creds = await _register_and_login(client, "sumtest")
    cookies = creds["cookies"]

    async_session_maker = postgres.get_session_maker(postgres.engine)
    async with async_session_maker() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_username(creds["username"])
        assert user is not None
        user_id = user.id

        workspace_repo = WorkspaceRepository(session)
        workspaces = await workspace_repo.list_user_workspaces(user_id)
        workspace_id = workspaces[0].id

        # Instantiate repositories & service
        summary_repo = WeeklySummaryRepository(session)
        notification_repo = NotificationRepository(session)
        notification_service = NotificationService(notification_repo)
        service = WeeklySummaryService(summary_repo, session, notification_service)

        # 1. Test generate_for_workspace_week directly
        # Let's generate for today's week
        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        session.add_all([
            PortfolioSnapshot(
                workspace_id=workspace_id,
                snapshot_date=week_start - timedelta(days=1),
                total_value="1050.00",
                total_cost="800.00",
                holdings_value="1000.00",
                cash_value="50.00",
                currency_code="INR",
            ),
            PortfolioSnapshot(
                workspace_id=workspace_id,
                snapshot_date=week_start + timedelta(days=6),
                total_value="1280.00",
                total_cost="800.00",
                holdings_value="1200.00",
                cash_value="80.00",
                currency_code="INR",
            ),
        ])
        await session.flush()

        summary = await service.generate_for_workspace_week(workspace_id, user_id, week_start)
        assert summary is not None
        assert summary.workspace_id == workspace_id
        assert summary.week_start == week_start
        assert summary.investing_summary == {
            "status": "complete",
            "portfolio_value_start": "1000.00",
            "portfolio_value_end": "1200.00",
            "cash_start": "50.00",
            "cash_end": "80.00",
            "week_change": "200.00",
            "week_change_pct": "20.00",
            "currency": "INR",
            "start_snapshot_date": (week_start - timedelta(days=1)).isoformat(),
            "end_snapshot_date": (week_start + timedelta(days=6)).isoformat(),
        }
        await session.commit()

    # 2. Test list weekly summaries endpoint
    list_resp = await client.get("/v1/summaries/weekly", cookies=cookies)
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) == 1
    assert items[0]["week_start"] == week_start.isoformat()
    summary_id = items[0]["public_id"]

    # 3. Test get weekly summary by public ID endpoint
    detail_resp = await client.get(f"/v1/summaries/weekly/{summary_id}", cookies=cookies)
    assert detail_resp.status_code == 200
    assert detail_resp.json()["public_id"] == summary_id

    # 4. Test get latest weekly summary endpoint
    latest_resp = await client.get("/v1/summaries/weekly/latest", cookies=cookies)
    assert latest_resp.status_code == 200
    assert latest_resp.json()["public_id"] == summary_id

    # 5. Test weekly_summary_job background scheduler task execution
    # Let's invoke weekly_summary_job which will trigger generation for last week
    await weekly_summary_job()

    # Let's verify that another summary was generated for the previous week
    # Calculate last week's Monday
    today = date.today()
    days_since_monday = today.weekday()
    last_monday = today - timedelta(days=days_since_monday + 7)

    list_resp = await client.get("/v1/summaries/weekly", cookies=cookies)
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    # We expect 2 summaries now (one created manually, one from weekly_summary_job)
    assert len(items) == 2

    # Confirm last_monday summary exists in items
    week_starts = [item["week_start"] for item in items]
    assert last_monday.isoformat() in week_starts
