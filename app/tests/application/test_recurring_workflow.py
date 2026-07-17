"""
Integration tests for process_workspace_recurring_transactions workflow (Spec 013).

Uses a real database (same pattern as test_budget_guardrails.py).
Workspace IDs in the 800 range to avoid collisions with other test suites.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.application.workflows import process_workspace_recurring_transactions
from app.auth.models import User
from app.core.audit import AuditLog
from app.core.database import postgres
from app.finance.models import Account, AccountType
from app.finance.repository import FinanceSettingRepository
from app.platform.models import Workspace, WorkspaceMembership
from app.spending.models import (
    RecurringTransaction,
    SpendingCategory,
    SpendingTransaction,
    TransactionType,
)

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_workspace(
    session, workspace_id: int, user_id: int, name: str, email: str, username: str
):
    user = User(
        id=user_id,
        email=email,
        username=username,
        hashed_password="hashed_password_here",
    )
    session.add(user)
    workspace = Workspace(id=workspace_id, name=name)
    session.add(workspace)
    await session.flush()
    membership = WorkspaceMembership(workspace_id=workspace_id, user_id=user_id, role="owner")
    session.add(membership)


async def _seed_category(session, workspace_id: int, name: str) -> SpendingCategory:
    cat = SpendingCategory(
        workspace_id=workspace_id,
        name=name,
        normalized_name=name.lower(),
        is_system=False,
    )
    session.add(cat)
    await session.flush()
    return cat


async def _seed_account(
    session, workspace_id: int, name: str, *, is_active: bool = True
) -> Account:
    account = Account(
        workspace_id=workspace_id,
        name=name,
        account_type=AccountType.wallet,
        default_currency_code="USD",
        is_active=is_active,
    )
    session.add(account)
    await session.flush()
    return account


async def _seed_recurrence(
    session,
    workspace_id: int,
    user_id: int,
    category_id: int,
    *,
    frequency: str = "monthly",
    interval: int = 1,
    next_due_date: date,
    end_date: date | None = None,
    amount: Decimal = Decimal("100.00"),
    tx_type: TransactionType = TransactionType.expense,
    account_id: int | None = None,
) -> RecurringTransaction:
    anchor = next_due_date  # simplification for tests
    recurrence = RecurringTransaction(
        workspace_id=workspace_id,
        user_id=user_id,
        category_id=category_id,
        account_id=account_id,
        amount=amount,
        type=tx_type,
        frequency=frequency,
        interval=interval,
        anchor_date=anchor,
        next_due_date=next_due_date,
        end_date=end_date,
        is_active=True,
    )
    session.add(recurrence)
    await session.flush()
    return recurrence


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recurring_basic_generation(override_database_url):
    """Single due recurring rule → one SpendingTransaction created, next_due_date advanced."""
    workspace_id = 801
    user_id = 81
    today = datetime.now(UTC).date()

    async with postgres.async_session_maker() as session:
        await _seed_workspace(
            session,
            workspace_id,
            user_id,
            name="Basic Gen WS",
            email="basic@example.com",
            username="basicuser",
        )
        cat = await _seed_category(session, workspace_id, "Rent")
        await _seed_recurrence(
            session,
            workspace_id,
            user_id,
            cat.id,
            frequency="monthly",
            interval=1,
            next_due_date=today,
        )
        await session.commit()

    async with postgres.async_session_maker() as session, session.begin():
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        count = await process_workspace_recurring_transactions(session, workspace)

    assert count == 1

    # Verify a SpendingTransaction was created
    async with postgres.async_session_maker() as session:
        txs = (
            (
                await session.execute(
                    select(SpendingTransaction).where(
                        SpendingTransaction.workspace_id == workspace_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(txs) == 1
        assert txs[0].amount == Decimal("100.00")
        assert txs[0].type == TransactionType.expense

        # Verify next_due_date was advanced
        rec = (
            await session.execute(
                select(RecurringTransaction).where(
                    RecurringTransaction.workspace_id == workspace_id
                )
            )
        ).scalar_one()
        assert rec.next_due_date > today
        assert rec.last_generated_at is not None

        # Verify audit log
        audits = (
            (await session.execute(select(AuditLog).where(AuditLog.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        assert len(audits) == 1
        assert audits[0].action == "recurring_transaction_generated"
        assert audits[0].details["before"] is None
        assert "amount" in audits[0].details["after"]


@pytest.mark.asyncio
async def test_recurring_catchup_multiple_periods(override_database_url):
    """next_due_date 3 months ago → 3 SpendingTransactions generated (catch-up)."""
    workspace_id = 802
    user_id = 82
    today = datetime.now(UTC).date()

    async with postgres.async_session_maker() as session:
        await _seed_workspace(
            session,
            workspace_id,
            user_id,
            name="Catchup WS",
            email="catchup@example.com",
            username="catchupuser",
        )
        cat = await _seed_category(session, workspace_id, "Netflix")
        await _seed_recurrence(
            session,
            workspace_id,
            user_id,
            cat.id,
            frequency="weekly",
            interval=1,
            next_due_date=today - timedelta(days=20),
            amount=Decimal("15.99"),
        )
        await session.commit()

    async with postgres.async_session_maker() as session, session.begin():
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        count = await process_workspace_recurring_transactions(session, workspace)

    # At least 3 catch-up transactions
    assert count >= 3

    async with postgres.async_session_maker() as session:
        txs = (
            (
                await session.execute(
                    select(SpendingTransaction).where(
                        SpendingTransaction.workspace_id == workspace_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(txs) >= 3
        assert all(tx.amount == Decimal("15.99") for tx in txs)
        assert all(tx.recurring_transaction_id is not None for tx in txs)


@pytest.mark.asyncio
async def test_recurring_idempotency(override_database_url):
    """Running the workflow twice on the same day creates no duplicate transactions."""
    workspace_id = 803
    user_id = 83
    today = datetime.now(UTC).date()

    async with postgres.async_session_maker() as session:
        await _seed_workspace(
            session,
            workspace_id,
            user_id,
            name="Idempotency WS",
            email="idem@example.com",
            username="idemuser",
        )
        cat = await _seed_category(session, workspace_id, "Salary")
        await _seed_recurrence(
            session,
            workspace_id,
            user_id,
            cat.id,
            frequency="monthly",
            interval=1,
            next_due_date=today,
            tx_type=TransactionType.income,
        )
        await session.commit()

    # First run
    async with postgres.async_session_maker() as session, session.begin():
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        await process_workspace_recurring_transactions(session, workspace)

    # Second run — next_due_date is now in the future, should generate nothing
    async with postgres.async_session_maker() as session, session.begin():
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        count = await process_workspace_recurring_transactions(session, workspace)

    assert count == 0

    async with postgres.async_session_maker() as session:
        txs = (
            (
                await session.execute(
                    select(SpendingTransaction).where(
                        SpendingTransaction.workspace_id == workspace_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(txs) == 1  # Only one from first run


@pytest.mark.asyncio
async def test_recurring_end_date_deactivation(override_database_url):
    """Rule whose next_due_date == end_date generates one transaction and deactivates."""
    workspace_id = 804
    user_id = 84
    today = datetime.now(UTC).date()

    async with postgres.async_session_maker() as session:
        await _seed_workspace(
            session,
            workspace_id,
            user_id,
            name="End Date WS",
            email="enddate@example.com",
            username="enddateuser",
        )
        cat = await _seed_category(session, workspace_id, "Trial")
        await _seed_recurrence(
            session,
            workspace_id,
            user_id,
            cat.id,
            frequency="monthly",
            interval=1,
            next_due_date=today,
            end_date=today,  # Expires after one final generation
        )
        await session.commit()

    async with postgres.async_session_maker() as session, session.begin():
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        count = await process_workspace_recurring_transactions(session, workspace)

    assert count == 1

    async with postgres.async_session_maker() as session:
        rec = (
            await session.execute(
                select(RecurringTransaction).where(
                    RecurringTransaction.workspace_id == workspace_id
                )
            )
        ).scalar_one()
        # Rule should be deactivated since next_due_date would exceed end_date
        assert not rec.is_active


@pytest.mark.asyncio
async def test_recurring_no_members_workspace_skipped(override_database_url):
    """Workspace with no members → workflow returns 0 without creating anything."""
    workspace_id = 805

    async with postgres.async_session_maker() as session:
        workspace = Workspace(id=workspace_id, name="No Members WS")
        session.add(workspace)
        await session.commit()

    async with postgres.async_session_maker() as session, session.begin():
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        count = await process_workspace_recurring_transactions(session, workspace)

    assert count == 0


@pytest.mark.asyncio
async def test_recurring_cross_workspace_isolation(override_database_url):
    """A workspace's recurring rules must not be generated by another workspace's workflow run."""
    workspace_a_id = 806
    workspace_b_id = 807
    user_a_id = 86
    user_b_id = 87
    today = datetime.now(UTC).date()

    async with postgres.async_session_maker() as session:
        await _seed_workspace(
            session,
            workspace_a_id,
            user_a_id,
            name="WS-A",
            email="wsa@example.com",
            username="wsauser",
        )
        await _seed_workspace(
            session,
            workspace_b_id,
            user_b_id,
            name="WS-B",
            email="wsb@example.com",
            username="wsbuser",
        )
        cat_a = await _seed_category(session, workspace_a_id, "Rent A")
        await _seed_recurrence(
            session,
            workspace_a_id,
            user_a_id,
            cat_a.id,
            next_due_date=today,
        )
        await session.commit()

    # Only run workflow for workspace B (has no recurring rules)
    async with postgres.async_session_maker() as session, session.begin():
        workspace_b = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_b_id))
        ).scalar_one()
        count = await process_workspace_recurring_transactions(session, workspace_b)

    assert count == 0

    # Workspace A's rule should not have been touched
    async with postgres.async_session_maker() as session:
        rec = (
            await session.execute(
                select(RecurringTransaction).where(
                    RecurringTransaction.workspace_id == workspace_a_id
                )
            )
        ).scalar_one()
        assert rec.next_due_date == today  # unchanged — never processed
        assert rec.last_generated_at is None


# ---------------------------------------------------------------------------
# Account resolution on generation (spec-084)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recurring_generation_sets_account_from_recurrence(override_database_url):
    """A recurring rule with an account_id propagates it onto the generated transaction."""
    workspace_id = 808
    user_id = 88
    today = datetime.now(UTC).date()

    async with postgres.async_session_maker() as session:
        await _seed_workspace(
            session,
            workspace_id,
            user_id,
            name="Account Propagation WS",
            email="acctprop@example.com",
            username="acctpropuser",
        )
        cat = await _seed_category(session, workspace_id, "Rent")
        account = await _seed_account(session, workspace_id, "Checking")
        await _seed_recurrence(
            session,
            workspace_id,
            user_id,
            cat.id,
            next_due_date=today,
            account_id=account.id,
        )
        await session.commit()
        account_id = account.id

    async with postgres.async_session_maker() as session, session.begin():
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        count = await process_workspace_recurring_transactions(session, workspace)

    assert count == 1

    async with postgres.async_session_maker() as session:
        tx = (
            await session.execute(
                select(SpendingTransaction).where(SpendingTransaction.workspace_id == workspace_id)
            )
        ).scalar_one()
        assert tx.account_id == account_id


@pytest.mark.asyncio
async def test_recurring_generation_falls_back_to_default_when_linked_account_deactivated(
    override_database_url,
):
    """If the recurrence's linked account was deactivated after the fact, generation
    falls back to the workspace's current default spending account rather than
    posting to (or failing on) the dead account."""
    workspace_id = 809
    user_id = 89
    today = datetime.now(UTC).date()

    async with postgres.async_session_maker() as session:
        await _seed_workspace(
            session,
            workspace_id,
            user_id,
            name="Deactivated Account Fallback WS",
            email="deactfallback@example.com",
            username="deactfallbackuser",
        )
        cat = await _seed_category(session, workspace_id, "Rent")
        dead_account = await _seed_account(session, workspace_id, "Dead", is_active=False)
        default_account = await _seed_account(session, workspace_id, "Default")
        await session.flush()
        setting_repo = FinanceSettingRepository(session)
        await setting_repo.upsert_workspace_settings(
            workspace_id,
            reporting_currency_code="USD",
            currency_display_preference=None,
            default_spending_account_id=default_account.id,
        )
        await _seed_recurrence(
            session,
            workspace_id,
            user_id,
            cat.id,
            next_due_date=today,
            account_id=dead_account.id,
        )
        await session.commit()
        default_account_id = default_account.id

    async with postgres.async_session_maker() as session, session.begin():
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        count = await process_workspace_recurring_transactions(session, workspace)

    assert count == 1

    async with postgres.async_session_maker() as session:
        tx = (
            await session.execute(
                select(SpendingTransaction).where(SpendingTransaction.workspace_id == workspace_id)
            )
        ).scalar_one()
        assert tx.account_id == default_account_id


@pytest.mark.asyncio
async def test_recurring_generation_null_account_for_legacy_rule_without_default(
    override_database_url,
):
    """Pre-existing (legacy) recurring rules with no account_id and no workspace
    default keep generating NULL-account transactions — not retroactive."""
    workspace_id = 810
    user_id = 90
    today = datetime.now(UTC).date()

    async with postgres.async_session_maker() as session:
        await _seed_workspace(
            session,
            workspace_id,
            user_id,
            name="Legacy No Account WS",
            email="legacynoaccount@example.com",
            username="legacynoaccountuser",
        )
        cat = await _seed_category(session, workspace_id, "Rent")
        await _seed_recurrence(
            session,
            workspace_id,
            user_id,
            cat.id,
            next_due_date=today,
        )
        await session.commit()

    async with postgres.async_session_maker() as session, session.begin():
        workspace = (
            await session.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalar_one()
        count = await process_workspace_recurring_transactions(session, workspace)

    assert count == 1

    async with postgres.async_session_maker() as session:
        tx = (
            await session.execute(
                select(SpendingTransaction).where(SpendingTransaction.workspace_id == workspace_id)
            )
        ).scalar_one()
        assert tx.account_id is None
