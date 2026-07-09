from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.application.jobs import weekly_summary_job
from app.auth.repository import UserRepository
from app.core.database import postgres
from app.finance.models import Account, AccountType
from app.health.models import Medication, MedicationEvent, WeightEntry
from app.investing.models import PortfolioSnapshot
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService
from app.platform.repository import WorkspaceRepository
from app.spending.models import SpendingBudget, SpendingCategory, SpendingTransaction
from app.summaries.repository import WeeklySummaryRepository
from app.summaries.service import WeeklySummaryService
from app.tests.integration.test_spending import _register_and_login
from app.todo.models import Todo


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
        start_dt = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)
        end_dt = start_dt + timedelta(days=7)

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
            # Add test Todo records to verify historical reconstruction logic
            Todo(
                workspace_id=workspace_id,
                user_id=user_id,
                title="Todo 1",
                completed=True,
                created_at=start_dt - timedelta(days=5),
                updated_at=start_dt - timedelta(days=2),
                due_date=start_dt - timedelta(days=3),
            ),
            Todo(
                workspace_id=workspace_id,
                user_id=user_id,
                title="Todo 2",
                completed=True,
                created_at=start_dt - timedelta(days=2),
                updated_at=start_dt + timedelta(days=2),
                due_date=start_dt - timedelta(days=1),
            ),
            Todo(
                workspace_id=workspace_id,
                user_id=user_id,
                title="Todo 3",
                completed=False,
                created_at=start_dt - timedelta(days=3),
                updated_at=start_dt - timedelta(days=3),
                due_date=start_dt + timedelta(days=2),
            ),
            Todo(
                workspace_id=workspace_id,
                user_id=user_id,
                title="Todo 4",
                completed=True,
                created_at=start_dt + timedelta(days=1),
                updated_at=end_dt + timedelta(days=1),
                due_date=start_dt + timedelta(days=3),
            ),
            Todo(
                workspace_id=workspace_id,
                user_id=user_id,
                title="Todo 5",
                completed=True,
                created_at=start_dt + timedelta(days=2),
                updated_at=start_dt + timedelta(days=4),
                due_date=start_dt + timedelta(days=3),
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
        assert summary.todo_summary["tasks_created"] == 2
        assert summary.todo_summary["tasks_completed"] == 2
        assert summary.todo_summary["tasks_overdue"] == 2
        assert summary.todo_summary["open_count_start"] == 2
        assert summary.todo_summary["open_count_end"] == 2
        assert summary.todo_summary["completion_rate_pct"] == "100.0"
        assert summary.spending_summary["status"] == "complete"
        assert summary.spending_summary["has_multiple_currencies"] is False
        summary_public_id = summary.public_id

        end_snapshot = (
            await session.execute(
                select(PortfolioSnapshot).where(
                    PortfolioSnapshot.workspace_id == workspace_id,
                    PortfolioSnapshot.snapshot_date == week_start + timedelta(days=6),
                )
            )
        ).scalar_one()
        end_snapshot.holdings_value = Decimal("1250.00")
        end_snapshot.total_value = Decimal("1330.00")
        regenerated = await service.generate_for_workspace_week(workspace_id, user_id, week_start)
        assert regenerated.public_id == summary_public_id
        assert regenerated.investing_summary["portfolio_value_end"] == "1250.00"
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


@pytest.mark.asyncio
async def test_weekly_summary_computes_budget_breached_for_categorized_spend(
    client: AsyncClient,
):
    creds = await _register_and_login(client, "sumbudget")

    async_session_maker = postgres.get_session_maker(postgres.engine)
    async with async_session_maker() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_username(creds["username"])
        assert user is not None
        user_id = user.id

        workspace_repo = WorkspaceRepository(session)
        workspaces = await workspace_repo.list_user_workspaces(user_id)
        workspace_id = workspaces[0].id

        category = (
            (
                await session.execute(
                    select(SpendingCategory).where(SpendingCategory.workspace_id == workspace_id)
                )
            )
            .scalars()
            .first()
        )

        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        start_dt = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)

        account = Account(
            workspace_id=workspace_id,
            name="Test Wallet",
            account_type=AccountType.wallet,
            default_currency_code="INR",
        )
        session.add(account)
        await session.flush()

        session.add(
            SpendingBudget(
                workspace_id=workspace_id,
                category_id=category.id,
                amount=Decimal("50.00"),
                start_month=week_start.replace(day=1),
            )
        )
        session.add(
            SpendingTransaction(
                workspace_id=workspace_id,
                user_id=user_id,
                account_id=account.id,
                category_id=category.id,
                amount=Decimal("75.00"),
                type="expense",
                occurred_at=start_dt + timedelta(days=1),
            )
        )
        await session.flush()

        summary_repo = WeeklySummaryRepository(session)
        notification_repo = NotificationRepository(session)
        notification_service = NotificationService(notification_repo)
        service = WeeklySummaryService(summary_repo, session, notification_service)

        summary = await service.generate_for_workspace_week(workspace_id, user_id, week_start)
        await session.commit()

    assert summary.spending_summary["status"] == "complete"
    assert summary.spending_summary["budgets_breached"] == 1
    assert summary.spending_summary["budget_utilization_pct"] == "150.0"
    assert summary.spending_summary["top_categories"][0]["amount"] == "75.00"


@pytest.mark.asyncio
async def test_weekly_summary_health_summary_omitted_without_health_data(client: AsyncClient):
    creds = await _register_and_login(client, "sumhealthnone")

    async_session_maker = postgres.get_session_maker(postgres.engine)
    async with async_session_maker() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_username(creds["username"])
        user_id = user.id
        workspace_repo = WorkspaceRepository(session)
        workspace_id = (await workspace_repo.list_user_workspaces(user_id))[0].id

        today = date.today()
        week_start = today - timedelta(days=today.weekday())

        summary_repo = WeeklySummaryRepository(session)
        notification_repo = NotificationRepository(session)
        notification_service = NotificationService(notification_repo)
        service = WeeklySummaryService(summary_repo, session, notification_service)

        summary = await service.generate_for_workspace_week(workspace_id, user_id, week_start)
        await session.commit()

    assert summary.health_summary is None


@pytest.mark.asyncio
async def test_weekly_summary_computes_health_summary(client: AsyncClient):
    creds = await _register_and_login(client, "sumhealthdata")

    async_session_maker = postgres.get_session_maker(postgres.engine)
    async with async_session_maker() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_username(creds["username"])
        user_id = user.id
        workspace_repo = WorkspaceRepository(session)
        workspace_id = (await workspace_repo.list_user_workspaces(user_id))[0].id

        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        start_dt = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)

        med = Medication(
            workspace_id=workspace_id,
            user_id=user_id,
            name="Metformin",
            frequency="daily",
            interval=1,
            anchor_date=week_start,
            timezone="UTC",
            times=["09:00"],
            is_active=True,
        )
        session.add(med)
        await session.flush()

        # Log one dose taken (day 0) — the rest of the week's slots are
        # unlogged and count as "missed" (past, no event).
        session.add(
            MedicationEvent(
                workspace_id=workspace_id,
                user_id=user_id,
                medication_id=med.id,
                scheduled_for=start_dt.replace(hour=9),
                status="taken",
            )
        )
        session.add_all([
            WeightEntry(
                workspace_id=workspace_id,
                user_id=user_id,
                measured_at=start_dt,
                weight_kg=Decimal("80.0"),
            ),
            WeightEntry(
                workspace_id=workspace_id,
                user_id=user_id,
                measured_at=start_dt + timedelta(days=2),
                weight_kg=Decimal("79.5"),
            ),
        ])
        await session.flush()

        summary_repo = WeeklySummaryRepository(session)
        notification_repo = NotificationRepository(session)
        notification_service = NotificationService(notification_repo)
        service = WeeklySummaryService(summary_repo, session, notification_service)

        summary = await service.generate_for_workspace_week(workspace_id, user_id, week_start)
        await session.commit()

    assert summary.health_summary is not None
    assert summary.health_summary["doses_scheduled"] == 7
    assert summary.health_summary["doses_taken"] == 1
    assert summary.health_summary["weight_entries_logged"] == 2
    assert summary.health_summary["weight_delta_kg"] == "-0.50"
