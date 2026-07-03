from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.application.insights import generate_workspace_insights
from app.auth.models import User
from app.core.database import postgres
from app.notifications.models import Notification
from app.platform.models import Workspace, WorkspaceMembership
from app.spending.models import (
    RecurringTransaction,
    SpendingBudget,
    SpendingCategory,
    SpendingTransaction,
)


async def _seed_workspace(session, workspace_id: int, user_id: int, name: str) -> None:
    session.add(
        User(
            id=user_id,
            email=f"{name.lower()}@example.com",
            username=name.lower(),
            hashed_password="hashed_password_here",
        )
    )
    session.add(Workspace(id=workspace_id, name=name))
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace_id, user_id=user_id, role="owner"))
    await session.flush()


async def _seed_category(session, workspace_id: int, name: str) -> SpendingCategory:
    cat = SpendingCategory(
        workspace_id=workspace_id, name=name, normalized_name=name.lower(), is_system=False
    )
    session.add(cat)
    await session.flush()
    return cat


def _txn(
    workspace_id: int, user_id: int, category_id: int, amount: str, occurred_at: datetime
) -> SpendingTransaction:
    return SpendingTransaction(
        workspace_id=workspace_id,
        user_id=user_id,
        category_id=category_id,
        amount=Decimal(amount),
        type="expense",
        occurred_at=occurred_at,
    )


async def _notifications(session, workspace_id: int) -> list[Notification]:
    return list(
        (
            await session.execute(
                select(Notification).where(
                    Notification.workspace_id == workspace_id, Notification.category == "insight"
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_spending_anomaly_triggers_and_dedupes(override_database_url):
    workspace_id, user_id = 960, 60
    today = date(2026, 7, 4)

    async with postgres.async_session_maker() as session:
        await _seed_workspace(session, workspace_id, user_id, "AnomalyWs")
        cat = await _seed_category(session, workspace_id, "Shopping")

        # Trailing 4-week baseline: $1000/week average.
        for days_ago in (10, 17, 24, 31):
            session.add(
                _txn(
                    workspace_id,
                    user_id,
                    cat.id,
                    "1000.00",
                    datetime.combine(today, datetime.min.time(), tzinfo=UTC)
                    - timedelta(days=days_ago),
                )
            )
        # Current week: a $3000 spike (3x baseline, well past the $500 floor).
        session.add(
            _txn(
                workspace_id,
                user_id,
                cat.id,
                "3000.00",
                datetime.combine(today, datetime.min.time(), tzinfo=UTC) - timedelta(days=2),
            )
        )
        await session.commit()

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await generate_workspace_insights(session, workspace, today=today)
        await session.commit()

    async with postgres.async_session_maker() as session:
        notifications = await _notifications(session, workspace_id)
        assert len(notifications) == 1
        assert notifications[0].entity_type == "spending_category_anomaly"

    # Re-run the same week — must not duplicate.
    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await generate_workspace_insights(session, workspace, today=today)
        await session.commit()

    async with postgres.async_session_maker() as session:
        notifications = await _notifications(session, workspace_id)
        assert len(notifications) == 1


@pytest.mark.asyncio
async def test_spending_anomaly_skips_category_with_no_baseline(override_database_url):
    workspace_id, user_id = 961, 61
    today = date(2026, 7, 4)

    async with postgres.async_session_maker() as session:
        await _seed_workspace(session, workspace_id, user_id, "NewCatWs")
        cat = await _seed_category(session, workspace_id, "BrandNew")
        session.add(
            _txn(
                workspace_id,
                user_id,
                cat.id,
                "500.00",
                datetime.combine(today, datetime.min.time(), tzinfo=UTC),
            )
        )
        await session.commit()

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await generate_workspace_insights(session, workspace, today=today)
        await session.commit()

    async with postgres.async_session_maker() as session:
        assert await _notifications(session, workspace_id) == []


@pytest.mark.asyncio
async def test_budget_pace_triggers_and_dedupes(override_database_url):
    workspace_id, user_id = 962, 62
    today = date(2026, 6, 15)
    month_start = date(2026, 6, 1)

    async with postgres.async_session_maker() as session:
        await _seed_workspace(session, workspace_id, user_id, "PaceWs")
        cat = await _seed_category(session, workspace_id, "Rent")
        session.add(
            SpendingBudget(
                workspace_id=workspace_id,
                category_id=cat.id,
                amount=Decimal("1000.00"),
                month_start=month_start,
            )
        )
        session.add(
            _txn(
                workspace_id,
                user_id,
                cat.id,
                "700.00",
                datetime(2026, 6, 5, tzinfo=UTC),
            )
        )
        await session.commit()

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await generate_workspace_insights(session, workspace, today=today)
        await session.commit()

    async with postgres.async_session_maker() as session:
        notifications = await _notifications(session, workspace_id)
        assert len(notifications) == 1
        assert notifications[0].entity_type == "spending_budget_pace"

    # Later the same month, pace worsens further — still only one notification.
    async with postgres.async_session_maker() as session:
        cat_row = (
            await session.execute(
                select(SpendingCategory).where(
                    SpendingCategory.workspace_id == workspace_id,
                    SpendingCategory.name == "Rent",
                )
            )
        ).scalar_one()
        session.add(
            _txn(workspace_id, user_id, cat_row.id, "200.00", datetime(2026, 6, 20, tzinfo=UTC))
        )
        await session.commit()

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await generate_workspace_insights(session, workspace, today=date(2026, 6, 25))
        await session.commit()

    async with postgres.async_session_maker() as session:
        assert len(await _notifications(session, workspace_id)) == 1


@pytest.mark.asyncio
async def test_recurring_charge_detected_then_suppressed_once_tracked(override_database_url):
    workspace_id, user_id = 963, 63
    today = date(2026, 7, 4)

    async with postgres.async_session_maker() as session:
        await _seed_workspace(session, workspace_id, user_id, "RecurringWs")
        cat = await _seed_category(session, workspace_id, "Streaming")
        session.add(_txn(workspace_id, user_id, cat.id, "49.99", datetime(2026, 5, 15, tzinfo=UTC)))
        session.add(_txn(workspace_id, user_id, cat.id, "49.99", datetime(2026, 6, 10, tzinfo=UTC)))
        await session.commit()
        cat_id = cat.id

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await generate_workspace_insights(session, workspace, today=today)
        await session.commit()

    async with postgres.async_session_maker() as session:
        notifications = await _notifications(session, workspace_id)
        assert len(notifications) == 1
        assert notifications[0].severity == "info"
        assert notifications[0].entity_type == "spending_category_recurring"

    # Now the user tracks it as a recurring rule — no new notification on rerun,
    # but the original one is not deleted.
    async with postgres.async_session_maker() as session:
        session.add(
            RecurringTransaction(
                workspace_id=workspace_id,
                user_id=user_id,
                category_id=cat_id,
                amount=Decimal("49.99"),
                type="expense",
                frequency="monthly",
                interval=1,
                anchor_date=date(2026, 5, 15),
                next_due_date=date(2026, 8, 15),
                is_active=True,
            )
        )
        await session.commit()

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await generate_workspace_insights(session, workspace, today=today)
        await session.commit()

    async with postgres.async_session_maker() as session:
        assert len(await _notifications(session, workspace_id)) == 1


@pytest.mark.asyncio
async def test_generate_workspace_insights_is_idempotent_across_all_detectors(
    override_database_url,
):
    workspace_id, user_id = 964, 64
    today = date(2026, 6, 20)
    month_start = date(2026, 6, 1)

    async with postgres.async_session_maker() as session:
        await _seed_workspace(session, workspace_id, user_id, "AllThreeWs")

        anomaly_cat = await _seed_category(session, workspace_id, "Dining")
        # Varied amounts (same $4000 sum / $1000 avg) so none of these land in
        # the same amount-tolerance bucket — otherwise this baseline would
        # also look like a recurring charge to Detector 3, since it spans
        # more than one calendar month.
        for days_ago, amount in ((10, "1300.00"), (17, "1100.00"), (24, "900.00"), (31, "700.00")):
            session.add(
                _txn(
                    workspace_id,
                    user_id,
                    anomaly_cat.id,
                    amount,
                    datetime.combine(today, datetime.min.time(), tzinfo=UTC)
                    - timedelta(days=days_ago),
                )
            )
        session.add(
            _txn(
                workspace_id,
                user_id,
                anomaly_cat.id,
                "3000.00",
                datetime.combine(today, datetime.min.time(), tzinfo=UTC) - timedelta(days=2),
            )
        )

        budget_cat = await _seed_category(session, workspace_id, "Utilities")
        session.add(
            SpendingBudget(
                workspace_id=workspace_id,
                category_id=budget_cat.id,
                amount=Decimal("1000.00"),
                month_start=month_start,
            )
        )
        session.add(
            _txn(workspace_id, user_id, budget_cat.id, "800.00", datetime(2026, 6, 5, tzinfo=UTC))
        )

        recurring_cat = await _seed_category(session, workspace_id, "Gym")
        session.add(
            _txn(workspace_id, user_id, recurring_cat.id, "29.99", datetime(2026, 5, 3, tzinfo=UTC))
        )
        session.add(
            _txn(workspace_id, user_id, recurring_cat.id, "29.99", datetime(2026, 6, 3, tzinfo=UTC))
        )
        await session.commit()

    for _ in range(2):
        async with postgres.async_session_maker() as session:
            workspace = (
                await session.execute(select(Workspace).where(Workspace.id == workspace_id))
            ).scalar_one()
            await generate_workspace_insights(session, workspace, today=today)
            await session.commit()

        async with postgres.async_session_maker() as session:
            notifications = await _notifications(session, workspace_id)
            assert len(notifications) == 3
            assert {n.entity_type for n in notifications} == {
                "spending_category_anomaly",
                "spending_budget_pace",
                "spending_category_recurring",
            }
