from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, select

from app.application.workflows import evaluate_workspace_budget_guardrails
from app.auth.models import User
from app.core.audit import AuditLog
from app.core.database import postgres
from app.platform.models import Workspace, WorkspaceMembership
from app.spending.models import SpendingBudget, SpendingCategory, SpendingTransaction
from app.todo.models import PriorityEnum, Todo


async def _seed_workspace(
    session, workspace_id: int, user_id: int, workspace_name: str, email: str, username: str
):
    user = User(
        id=user_id,
        email=email,
        username=username,
        hashed_password="hashed_password_here",
    )
    session.add(user)
    workspace = Workspace(id=workspace_id, name=workspace_name)
    session.add(workspace)
    await session.flush()
    membership = WorkspaceMembership(workspace_id=workspace_id, user_id=user_id, role="owner")
    session.add(membership)


async def _seed_category_budget_transaction(
    session,
    workspace_id: int,
    user_id: int,
    cat_name: str,
    budget_amount: float,
    spend_amount: float,
    month_start: date,
) -> int:
    cat = SpendingCategory(
        workspace_id=workspace_id,
        name=cat_name,
        normalized_name=cat_name.lower(),
        is_system=False,
    )
    session.add(cat)
    await session.flush()

    budget = SpendingBudget(
        workspace_id=workspace_id,
        category_id=cat.id,
        amount=budget_amount,
        start_month=month_start,
    )
    session.add(budget)

    if spend_amount > 0:
        tx = SpendingTransaction(
            workspace_id=workspace_id,
            user_id=user_id,
            category_id=cat.id,
            amount=spend_amount,
            type="expense",
            occurred_at=datetime.now(UTC),
        )
        session.add(tx)
    await session.flush()
    return cat.id


@pytest.mark.asyncio
async def test_budget_guardrails_state_machine(override_database_url):
    workspace_id = 901
    user_id = 9
    today = datetime.now(UTC).date()
    month_start = today.replace(day=1)

    async with postgres.async_session_maker() as session:
        await _seed_workspace(
            session,
            workspace_id=workspace_id,
            user_id=user_id,
            workspace_name="Guardrail Test WS",
            email="guard@example.com",
            username="guarduser",
        )
        cat_id = await _seed_category_budget_transaction(
            session,
            workspace_id=workspace_id,
            user_id=user_id,
            cat_name="Groceries",
            budget_amount=100.00,
            spend_amount=0.0,
            month_start=month_start,
        )
        await session.commit()

    # 1. Under Threshold (80% spend, threshold >= 90%)
    async with postgres.async_session_maker() as session:
        tx = SpendingTransaction(
            workspace_id=workspace_id,
            user_id=user_id,
            category_id=cat_id,
            amount=80.00,
            type="expense",
            occurred_at=datetime.now(UTC),
        )
        session.add(tx)
        await session.commit()

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await evaluate_workspace_budget_guardrails(session, workspace)
        await session.commit()

    async with postgres.async_session_maker() as session:
        todos = (
            (await session.execute(select(Todo).where(Todo.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        assert len(todos) == 0, "No todo under threshold"

    # 2. Warning Breach (95% spend, threshold >= 90%)
    async with postgres.async_session_maker() as session:
        # 80 + 15 = 95
        tx2 = SpendingTransaction(
            workspace_id=workspace_id,
            user_id=user_id,
            category_id=cat_id,
            amount=15.00,
            type="expense",
            occurred_at=datetime.now(UTC),
        )
        session.add(tx2)
        await session.commit()

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await evaluate_workspace_budget_guardrails(session, workspace)
        await session.commit()

    async with postgres.async_session_maker() as session:
        todos = (
            (await session.execute(select(Todo).where(Todo.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        assert len(todos) == 1
        todo = todos[0]
        assert "[Budget] Warning" in todo.title
        assert todo.priority == PriorityEnum.medium
        assert not todo.completed

        # Verify Audit Log
        audits = (
            (await session.execute(select(AuditLog).where(AuditLog.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        assert len(audits) == 1
        assert audits[0].action == "budget_guardrail_triggered"
        assert audits[0].details["before"] is None
        assert audits[0].details["after"]["title"] == todo.title

    # 3. Idempotency (no duplicate warning)
    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await evaluate_workspace_budget_guardrails(session, workspace)
        await session.commit()

    async with postgres.async_session_maker() as session:
        todos = (
            (await session.execute(select(Todo).where(Todo.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        assert len(todos) == 1
        audits = (
            (await session.execute(select(AuditLog).where(AuditLog.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        assert len(audits) == 1

    # 4. Critical Breach (105% spend, threshold >= 100%)
    async with postgres.async_session_maker() as session:
        # 95 + 10 = 105
        tx3 = SpendingTransaction(
            workspace_id=workspace_id,
            user_id=user_id,
            category_id=cat_id,
            amount=10.00,
            type="expense",
            occurred_at=datetime.now(UTC),
        )
        session.add(tx3)
        await session.commit()

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await evaluate_workspace_budget_guardrails(session, workspace)
        await session.commit()

    async with postgres.async_session_maker() as session:
        todos = (
            (await session.execute(select(Todo).where(Todo.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        assert len(todos) == 1
        todo = todos[0]
        assert "[Budget] Critical" in todo.title
        assert todo.priority == PriorityEnum.high
        assert not todo.completed

        # Verify Audit Log records escalation
        audits = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.workspace_id == workspace_id)
                    .order_by(AuditLog.timestamp.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 2
        assert audits[1].details["before"]["title"] == "[Budget] Warning: Groceries"
        assert audits[1].details["after"]["title"] == "[Budget] Critical: Groceries"

    # 5. Idempotency (no duplicate critical)
    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await evaluate_workspace_budget_guardrails(session, workspace)
        await session.commit()

    async with postgres.async_session_maker() as session:
        todos = (
            (await session.execute(select(Todo).where(Todo.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        assert len(todos) == 1
        audits = (
            (await session.execute(select(AuditLog).where(AuditLog.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        assert len(audits) == 2

    # 6. Auto-resolution (spend drops back below warning threshold)
    async with postgres.async_session_maker() as session:
        await session.execute(
            delete(SpendingTransaction).where(SpendingTransaction.workspace_id == workspace_id)
        )
        await session.commit()

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await evaluate_workspace_budget_guardrails(session, workspace)
        await session.commit()

    async with postgres.async_session_maker() as session:
        todos = (
            (await session.execute(select(Todo).where(Todo.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        assert len(todos) == 1
        todo = todos[0]
        assert todo.completed

        audits = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.workspace_id == workspace_id)
                    .order_by(AuditLog.timestamp.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 3
        assert audits[2].details["before"]["completed"] is False
        assert audits[2].details["after"]["completed"] is True


@pytest.mark.asyncio
async def test_budget_guardrails_edge_cases(override_database_url):
    workspace_id = 902
    user_id = 92
    today = datetime.now(UTC).date()
    month_start = today.replace(day=1)

    async with postgres.async_session_maker() as session:
        # Seed a workspace but do NOT add membership owner
        workspace = Workspace(id=workspace_id, name="No Owner WS")
        session.add(workspace)
        await session.commit()

    # 1. Workspace with no members (should return early without doing anything)
    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await evaluate_workspace_budget_guardrails(session, workspace)
        await session.commit()

        # Verify no todos or audit logs
        todos = (
            (await session.execute(select(Todo).where(Todo.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        assert len(todos) == 0

    # 2. Budgets with negative/zero amount should be skipped
    async with postgres.async_session_maker() as session:
        # Add owner
        membership = WorkspaceMembership(workspace_id=workspace_id, user_id=user_id, role="owner")
        session.add(membership)
        user = User(
            id=user_id,
            email="guard2@example.com",
            username="guarduser2",
            hashed_password="hashed_password_here",
        )
        session.add(user)

        # Seed budget with amount=0 and amount=-50
        cat1 = SpendingCategory(
            workspace_id=workspace_id,
            name="Zero Budget",
            normalized_name="zero budget",
            is_system=False,
        )
        cat2 = SpendingCategory(
            workspace_id=workspace_id,
            name="Negative Budget",
            normalized_name="negative budget",
            is_system=False,
        )
        session.add(cat1)
        session.add(cat2)
        await session.flush()

        b1 = SpendingBudget(
            workspace_id=workspace_id,
            category_id=cat1.id,
            amount=0.0,
            start_month=month_start,
        )
        b2 = SpendingBudget(
            workspace_id=workspace_id,
            category_id=cat2.id,
            amount=-50.0,
            start_month=month_start,
        )
        session.add(b1)
        session.add(b2)

        # Transactions spending money
        t1 = SpendingTransaction(
            workspace_id=workspace_id,
            user_id=user_id,
            category_id=cat1.id,
            amount=10.0,
            type="expense",
            occurred_at=datetime.now(UTC),
        )
        t2 = SpendingTransaction(
            workspace_id=workspace_id,
            user_id=user_id,
            category_id=cat2.id,
            amount=20.0,
            type="expense",
            occurred_at=datetime.now(UTC),
        )
        session.add(t1)
        session.add(t2)

        await session.commit()

    async with postgres.async_session_maker() as session:
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await evaluate_workspace_budget_guardrails(session, workspace)
        await session.commit()

        # Confirm no todos were created since budgets <= 0 are skipped
        todos = (
            (await session.execute(select(Todo).where(Todo.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        assert len(todos) == 0
