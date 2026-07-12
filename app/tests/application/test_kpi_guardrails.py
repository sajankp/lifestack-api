from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.application.workflows import evaluate_workspace_kpi_breaches
from app.auth.models import User
from app.core.database import postgres
from app.finance.models import Account, AccountType
from app.notifications.models import Notification
from app.platform.models import Workspace, WorkspaceMembership
from app.spending.models import (
    FinancialKpi,
    KpiMetricType,
    KpiTargetDirection,
    KpiWindow,
    SpendingCategory,
    SpendingTransaction,
)


async def _seed_workspace(session, workspace_id: int, user_id: int, email: str, username: str):
    user = User(id=user_id, email=email, username=username, hashed_password="hashed")
    session.add(user)
    workspace = Workspace(id=workspace_id, name=f"KPI WS {workspace_id}")
    session.add(workspace)
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace_id, user_id=user_id, role="owner"))
    await session.flush()
    return workspace


async def _seed_account(session, workspace_id: int, currency: str = "USD") -> Account:
    account = Account(
        workspace_id=workspace_id,
        name=f"Wallet-{currency}",
        account_type=AccountType.wallet,
        default_currency_code=currency,
    )
    session.add(account)
    await session.flush()
    return account


@pytest.mark.asyncio
async def test_kpi_breach_notified_once_then_idempotent(override_database_url):
    workspace_id = 951
    user_id = 51

    async with postgres.async_session_maker() as session:
        await _seed_workspace(session, workspace_id, user_id, "kpi1@example.com", "kpi1")
        account = await _seed_account(session, workspace_id)
        cat = SpendingCategory(
            workspace_id=workspace_id, name="Dining", normalized_name="dining", is_system=False
        )
        session.add(cat)
        await session.flush()

        kpi = FinancialKpi(
            workspace_id=workspace_id,
            name="Dining under 50",
            metric_type=KpiMetricType.spend_total,
            evaluation_window=KpiWindow.calendar_month,
            category_id=cat.id,
            currency_code="USD",
            target_value=50,
            target_direction=KpiTargetDirection.lte,
        )
        session.add(kpi)

        tx = SpendingTransaction(
            workspace_id=workspace_id,
            user_id=user_id,
            category_id=cat.id,
            account_id=account.id,
            amount=75,
            type="expense",
            occurred_at=datetime.now(UTC),
        )
        session.add(tx)
        await session.commit()

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await evaluate_workspace_kpi_breaches(session, workspace)
        await session.commit()

    async with postgres.async_session_maker() as session:
        notifications = (
            (
                await session.execute(
                    select(Notification).where(
                        Notification.workspace_id == workspace_id,
                        Notification.category == "kpi",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(notifications) == 1
        assert notifications[0].entity_type == "financial_kpi"
        assert "Dining under 50" in notifications[0].title

    # Re-running the same evaluation must not create a duplicate notification
    # within the same window (idempotency, mirrors budget guardrails).
    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await evaluate_workspace_kpi_breaches(session, workspace)
        await session.commit()

    async with postgres.async_session_maker() as session:
        notifications = (
            (
                await session.execute(
                    select(Notification).where(
                        Notification.workspace_id == workspace_id,
                        Notification.category == "kpi",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(notifications) == 1


@pytest.mark.asyncio
async def test_kpi_not_breached_does_not_notify(override_database_url):
    workspace_id = 952
    user_id = 52

    async with postgres.async_session_maker() as session:
        await _seed_workspace(session, workspace_id, user_id, "kpi2@example.com", "kpi2")
        account = await _seed_account(session, workspace_id)
        cat = SpendingCategory(
            workspace_id=workspace_id, name="Dining", normalized_name="dining", is_system=False
        )
        session.add(cat)
        await session.flush()

        kpi = FinancialKpi(
            workspace_id=workspace_id,
            name="Dining under 100",
            metric_type=KpiMetricType.spend_total,
            evaluation_window=KpiWindow.calendar_month,
            category_id=cat.id,
            currency_code="USD",
            target_value=100,
            target_direction=KpiTargetDirection.lte,
        )
        session.add(kpi)
        tx = SpendingTransaction(
            workspace_id=workspace_id,
            user_id=user_id,
            category_id=cat.id,
            account_id=account.id,
            amount=20,
            type="expense",
            occurred_at=datetime.now(UTC),
        )
        session.add(tx)
        await session.commit()

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await evaluate_workspace_kpi_breaches(session, workspace)
        await session.commit()

    async with postgres.async_session_maker() as session:
        notifications = (
            (
                await session.execute(
                    select(Notification).where(
                        Notification.workspace_id == workspace_id,
                        Notification.category == "kpi",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(notifications) == 0


@pytest.mark.asyncio
async def test_kpi_skipped_when_currency_constraint_now_violated(override_database_url):
    """A KPI created when the workspace was single-currency, evaluated after
    a second account with a different currency was added, must be skipped
    (re-checked at evaluation time) rather than raising — spec-077."""
    workspace_id = 953
    user_id = 53

    async with postgres.async_session_maker() as session:
        await _seed_workspace(session, workspace_id, user_id, "kpi3@example.com", "kpi3")
        await _seed_account(session, workspace_id, currency="USD")
        cat = SpendingCategory(
            workspace_id=workspace_id, name="Dining", normalized_name="dining", is_system=False
        )
        session.add(cat)
        await session.flush()

        # No account_id filter: this KPI evaluates over the whole workspace.
        kpi = FinancialKpi(
            workspace_id=workspace_id,
            name="All spend",
            metric_type=KpiMetricType.spend_total,
            evaluation_window=KpiWindow.calendar_month,
            currency_code="USD",
            target_value=10,
            target_direction=KpiTargetDirection.lte,
        )
        session.add(kpi)
        await session.commit()

        # Now the workspace becomes mixed-currency after the KPI was defined.
        await _seed_account(session, workspace_id, currency="INR")
        await session.commit()

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await evaluate_workspace_kpi_breaches(session, workspace)
        await session.commit()

    async with postgres.async_session_maker() as session:
        notifications = (
            (
                await session.execute(
                    select(Notification).where(
                        Notification.workspace_id == workspace_id,
                        Notification.category == "kpi",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(notifications) == 0
