import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.application.workflows import (
    process_workspace_recurring_todos,
    process_workspace_recurring_transactions,
)
from app.auth.models import User
from app.core.database import postgres
from app.core.recurrence import advance_due_date
from app.platform.models import Workspace, WorkspaceMembership
from app.spending.models import RecurringTransaction, SpendingCategory
from app.spending.models import TransactionType as SpendingTransactionType
from app.tests.integration.test_spending import _register_and_login
from app.todo.models import RecurringTodoRule, Todo


@pytest.mark.asyncio
async def test_todo_rule_nth_weekday_without_fields_rejected(client: AsyncClient):
    await _register_and_login(client, uuid.uuid4().hex[:8])
    res = await client.post(
        "/v1/todo/recurring/",
        json={
            "title": "Nth weekday missing fields",
            "frequency": "monthly",
            "interval": 1,
            "anchor_date": "2026-07-03",
            "monthly_mode": "nth_weekday",
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_todo_rule_monthly_mode_with_non_monthly_frequency_rejected(client: AsyncClient):
    await _register_and_login(client, uuid.uuid4().hex[:8])
    res = await client.post(
        "/v1/todo/recurring/",
        json={
            "title": "Bad mode/frequency combo",
            "frequency": "weekly",
            "interval": 1,
            "anchor_date": "2026-07-03",
            "monthly_mode": "last_day",
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_recurring_transaction_nth_weekday_without_fields_rejected(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    category_id = cats[0]["public_id"]

    res = await client.post(
        "/v1/spending/recurring",
        json={
            "category_id": category_id,
            "amount": "10.00",
            "type": "expense",
            "frequency": "monthly",
            "interval": 1,
            "anchor_date": "2026-07-03",
            "monthly_mode": "nth_weekday",
        },
        cookies=creds["cookies"],
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_recurring_transaction_monthly_mode_with_non_monthly_frequency_rejected(
    client: AsyncClient,
):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    category_id = cats[0]["public_id"]

    res = await client.post(
        "/v1/spending/recurring",
        json={
            "category_id": category_id,
            "amount": "10.00",
            "type": "expense",
            "frequency": "daily",
            "interval": 1,
            "anchor_date": "2026-07-03",
            "monthly_mode": "last_day",
        },
        cookies=creds["cookies"],
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_db_check_constraint_rejects_nth_weekday_without_fields(override_database_url):
    workspace_id, user_id = 970, 70

    async with postgres.async_session_maker() as session:
        session.add(
            User(
                id=user_id,
                email="checkconstraint@example.com",
                username="checkconstraint",
                hashed_password="hashed",
            )
        )
        session.add(Workspace(id=workspace_id, name="CheckConstraintWs"))
        await session.flush()
        session.add(WorkspaceMembership(workspace_id=workspace_id, user_id=user_id, role="owner"))
        cat = SpendingCategory(
            workspace_id=workspace_id, name="Rent", normalized_name="rent", is_system=False
        )
        session.add(cat)
        await session.flush()
        cat_id = cat.id
        await session.commit()

    async with postgres.async_session_maker() as session:
        session.add(
            RecurringTransaction(
                workspace_id=workspace_id,
                user_id=user_id,
                category_id=cat_id,
                amount=10,
                type=SpendingTransactionType.expense,
                frequency="monthly",
                interval=1,
                anchor_date=date(2026, 7, 3),
                next_due_date=date(2026, 7, 3),
                monthly_mode="nth_weekday",
                by_weekday=None,
                by_ordinal=None,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_end_to_end_generation_last_day_transaction_and_nth_weekday_todo(
    override_database_url,
):
    workspace_id, user_id = 971, 71

    async with postgres.async_session_maker() as session:
        session.add(
            User(
                id=user_id,
                email="e2erecurrence@example.com",
                username="e2erecurrence",
                hashed_password="hashed",
            )
        )
        session.add(Workspace(id=workspace_id, name="E2ERecurrenceWs"))
        await session.flush()
        session.add(WorkspaceMembership(workspace_id=workspace_id, user_id=user_id, role="owner"))
        cat = SpendingCategory(
            workspace_id=workspace_id, name="Rent", normalized_name="rent", is_system=False
        )
        session.add(cat)
        await session.flush()

        today = datetime.now(UTC).date()
        # Due today (or overdue), last_day monthly recurring transaction.
        session.add(
            RecurringTransaction(
                workspace_id=workspace_id,
                user_id=user_id,
                category_id=cat.id,
                amount=1500,
                type=SpendingTransactionType.expense,
                frequency="monthly",
                interval=1,
                anchor_date=today,
                next_due_date=today,
                monthly_mode="last_day",
            )
        )
        # Due today, nth_weekday (first Monday) monthly recurring todo.
        session.add(
            RecurringTodoRule(
                workspace_id=workspace_id,
                user_id=user_id,
                title="Weekly review",
                frequency="monthly",
                interval=1,
                anchor_date=today,
                next_due_date=today,
                monthly_mode="nth_weekday",
                by_weekday=0,
                by_ordinal=1,
            )
        )
        await session.commit()

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        tx_generated = await process_workspace_recurring_transactions(session, workspace)
        todo_generated = await process_workspace_recurring_todos(session, workspace)
        await session.commit()

    assert tx_generated == 1
    assert todo_generated == 1

    today = datetime.now(UTC).date()
    async with postgres.async_session_maker() as session:
        recurrence = (
            await session.execute(
                select(RecurringTransaction).where(
                    RecurringTransaction.workspace_id == workspace_id
                )
            )
        ).scalar_one()
        expected_tx_next = advance_due_date(today, "monthly", 1, monthly_mode="last_day")
        assert recurrence.next_due_date == expected_tx_next

        rule = (
            await session.execute(
                select(RecurringTodoRule).where(RecurringTodoRule.workspace_id == workspace_id)
            )
        ).scalar_one()
        expected_rule_next = advance_due_date(
            today, "monthly", 1, monthly_mode="nth_weekday", by_weekday=0, by_ordinal=1
        )
        assert rule.next_due_date == expected_rule_next

        todo_row = (
            await session.execute(select(Todo).where(Todo.workspace_id == workspace_id))
        ).scalar_one()
        assert todo_row.title == "Weekly review"


@pytest.mark.asyncio
async def test_recurring_transaction_created_mid_cycle_is_not_immediately_overdue(
    client: AsyncClient,
):
    """A monthly rule anchored to a past-in-the-cycle date shouldn't be born overdue.

    Regression for a rule created mid-month with anchor_date = month start: the raw
    anchor is already in the past relative to "today", so next_due_date must advance
    past the elapsed cycle instead of landing on a stale, already-past date.
    """
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    cats = (await client.get("/v1/spending/categories", cookies=creds["cookies"])).json()["items"]
    category_id = cats[0]["public_id"]
    account_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Wallet", "account_type": "wallet", "default_currency_code": "USD"},
        cookies=creds["cookies"],
    )
    assert account_res.status_code == 201, account_res.text
    account_id = account_res.json()["public_id"]

    today = datetime.now(UTC).date()
    past_anchor = today - timedelta(days=40)

    res = await client.post(
        "/v1/spending/recurring",
        json={
            "category_id": category_id,
            "account_id": account_id,
            "amount": "10.00",
            "type": "expense",
            "frequency": "monthly",
            "interval": 1,
            "anchor_date": past_anchor.isoformat(),
        },
        cookies=creds["cookies"],
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["anchor_date"] == past_anchor.isoformat()
    next_due = date.fromisoformat(body["next_due_date"])
    assert next_due >= today
    assert next_due != past_anchor


@pytest.mark.asyncio
async def test_recurring_todo_rule_created_mid_cycle_is_not_immediately_overdue(
    client: AsyncClient,
):
    await _register_and_login(client, uuid.uuid4().hex[:8])

    today = datetime.now(UTC).date()
    past_anchor = today - timedelta(days=40)

    res = await client.post(
        "/v1/todo/recurring/",
        json={
            "title": "Pay rent",
            "frequency": "monthly",
            "interval": 1,
            "anchor_date": past_anchor.isoformat(),
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["anchor_date"] == past_anchor.isoformat()
    next_due = date.fromisoformat(body["next_due_date"])
    assert next_due >= today
    assert next_due != past_anchor
