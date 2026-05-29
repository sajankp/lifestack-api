import pytest
from sqlalchemy import select

from app.auth.models import User
from app.capture.agent import execute_agent_tool
from app.core.audit import AuditLog
from app.core.database import postgres
from app.finance.models import Account, AccountType
from app.investing.models import CashBalance
from app.platform.models import Workspace, WorkspaceMembership
from app.spending.models import SpendingCategory, SpendingTransaction
from app.todo.models import Todo


@pytest.fixture
async def seed_agent_test_data(override_database_url):
    """Seed user, workspace, categories, and accounts for agent tests."""
    async with postgres.async_session_maker() as session:
        user = User(
            id=10,
            email="agent_test@example.com",
            username="agent_test",
            hashed_password="hashed_password_here",
        )
        session.add(user)

        ws = Workspace(id=20, name="Agent Workspace")
        session.add(ws)
        await session.flush()

        membership = WorkspaceMembership(workspace_id=20, user_id=10, role="owner")
        session.add(membership)

        # Seed categories
        cat_food = SpendingCategory(
            workspace_id=20, name="food", normalized_name="food", description="Food expenses"
        )
        cat_other = SpendingCategory(
            workspace_id=20, name="other", normalized_name="other", description="Other expenses"
        )
        session.add(cat_food)
        session.add(cat_other)
        await session.flush()

        # USD currency is already seeded by alembic migrations
        # Seed account
        account = Account(
            workspace_id=20,
            name="Chase Brokerage",
            default_currency_code="USD",
            account_type=AccountType.brokerage,
        )
        session.add(account)

        await session.commit()


@pytest.mark.asyncio
async def test_execute_agent_tool_create_todo(seed_agent_test_data):
    res = await execute_agent_tool(
        name="create_todo_task",
        args={"title": "Buy groceries tomorrow", "due_date": "2026-05-29", "priority": "high"},
        user_id=10,
        workspace_id=20,
    )

    assert res["status"] == "success"
    assert res["entity_type"] == "todo"
    assert res["title"] == "Buy groceries tomorrow"
    assert res["due_date"].startswith("2026-05")
    assert res["priority"] == "high"

    # Query DB to verify
    async with postgres.async_session_maker() as session:
        todos = (await session.execute(select(Todo).where(Todo.workspace_id == 20))).scalars().all()
        assert len(todos) == 1
        assert todos[0].title == "Buy groceries tomorrow"
        assert todos[0].priority == "high"

        # Verify audit logs
        logs = (
            (await session.execute(select(AuditLog).where(AuditLog.workspace_id == 20)))
            .scalars()
            .all()
        )
        assert len(logs) == 1
        assert logs[0].action == "create"
        assert logs[0].module == "todo"


@pytest.mark.asyncio
async def test_execute_agent_tool_log_spending(seed_agent_test_data):
    # Print the database state first
    async with postgres.async_session_maker() as session:
        db_cats = (
            (
                await session.execute(
                    select(SpendingCategory).where(SpendingCategory.workspace_id == 20)
                )
            )
            .scalars()
            .all()
        )
        print(
            "\nDB CATEGORIES BEFORE EXECUTION:",
            [(c.name, c.normalized_name, str(c.public_id)) for c in db_cats],
        )

    res = await execute_agent_tool(
        name="log_spending_transaction",
        args={"amount": "15.50", "category_name": "food", "description": "Lunch at restaurant"},
        user_id=10,
        workspace_id=20,
    )

    print("TOOL RESPONSE:", res)

    assert res["status"] == "success"
    assert res["entity_type"] == "transaction"
    assert res["amount"] == "15.50"
    assert res["category"].lower() == "food"
    assert res["description"] == "Lunch at restaurant"

    # Query DB to verify
    async with postgres.async_session_maker() as session:
        txs = (
            (
                await session.execute(
                    select(SpendingTransaction).where(SpendingTransaction.workspace_id == 20)
                )
            )
            .scalars()
            .all()
        )
        assert len(txs) == 1
        assert txs[0].amount == 15.50
        assert txs[0].description == "Lunch at restaurant"

        # Verify audit logs
        logs = (
            (await session.execute(select(AuditLog).where(AuditLog.workspace_id == 20)))
            .scalars()
            .all()
        )
        assert len(logs) == 1
        assert logs[0].action == "create"
        assert logs[0].module == "spending"


@pytest.mark.asyncio
async def test_execute_agent_tool_log_cash_balance(seed_agent_test_data):
    res = await execute_agent_tool(
        name="log_cash_balance",
        args={"account_name": "Chase Brokerage", "balance": "10500.25", "currency": "USD"},
        user_id=10,
        workspace_id=20,
    )

    assert res["status"] == "success"
    assert res["entity_type"] == "cash_balance"
    assert res["account_name"] == "Chase Brokerage"
    assert res["balance"] == "10500.25"
    assert res["currency"] == "USD"

    # Query DB to verify
    async with postgres.async_session_maker() as session:
        balances = (
            (await session.execute(select(CashBalance).where(CashBalance.workspace_id == 20)))
            .scalars()
            .all()
        )
        assert len(balances) == 1
        assert balances[0].account_name == "Chase Brokerage"
        assert balances[0].balance == 10500.25
        assert balances[0].currency == "USD"

        # Verify audit logs
        logs = (
            (await session.execute(select(AuditLog).where(AuditLog.workspace_id == 20)))
            .scalars()
            .all()
        )
        assert len(logs) == 1
        assert logs[0].action == "create"
        assert logs[0].module == "investing"


@pytest.mark.asyncio
async def test_execute_agent_tool_error_handling(seed_agent_test_data):
    res = await execute_agent_tool(name="unknown_tool", args={}, user_id=10, workspace_id=20)
    assert res["status"] == "error"
    assert "Unknown function" in res["message"]
