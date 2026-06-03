"""
Integration tests for the scheduler and budget guardrails workflow (Spec 005, Spec 009).

Test coverage:
  - Scheduler gating: verifies global scheduler is OFF when SCHEDULER_ENABLED=False,
    and that the job can be registered with the configured interval.
  - Budget guardrails E2E: warning → idempotency → critical → auto-resolve state machine.
  - Cross-workspace isolation: one workspace's breach does NOT create todos in another.
  - Per-workspace failure isolation: an exception in one workspace is caught and the
    remaining workspaces still complete successfully.
"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.application.jobs import budget_guardrails_job
from app.application.workflows import evaluate_workspace_budget_guardrails
from app.auth.models import User
from app.config import settings
from app.core.audit import AuditLog
from app.core.database import postgres
from app.core.scheduler import register_interval_job, scheduler
from app.platform.models import Workspace, WorkspaceMembership
from app.spending.models import SpendingBudget, SpendingCategory, SpendingTransaction
from app.todo.models import Todo

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _seed_workspace(
    session, workspace_id: int, user_id: int, workspace_name: str, email: str, username: str
):
    """Seed a workspace, user, and owner membership in one session."""
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
    month_start,
) -> int:
    """Seed a category + budget + single expense transaction. Returns category_id."""
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
        month_start=month_start,
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

    return cat.id


# ---------------------------------------------------------------------------
# Autouse fixture — seeds workspace 501 / user 1 for all tests in this file
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def seed_scheduler_test_data(override_database_url):
    """Seed the primary user and workspace used by most tests in this module."""
    async with postgres.async_session_maker() as session:
        await _seed_workspace(
            session,
            workspace_id=501,
            user_id=1,
            workspace_name="Scheduler Workspace",
            email="scheduler_actor@example.com",
            username="scheduler_actor",
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Fix 4: Scheduler gating test — tests app config behaviour, not APScheduler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_gating_and_registration():
    """
    Verify that:
      - SCHEDULER_ENABLED defaults to False in the test environment, so the
        global scheduler is not running.
      - The budget_guardrails_job can be registered with the correct interval
        derived from BUDGET_GUARDRAILS_INTERVAL_HOURS.
    """
    # The global scheduler must be idle because SCHEDULER_ENABLED=False
    assert settings.SCHEDULER_ENABLED is False, (
        "SCHEDULER_ENABLED must be False in the test environment"
    )
    assert not scheduler.running, (
        "Global scheduler must not be running when SCHEDULER_ENABLED=False"
    )

    # Verify the job can be registered with the configured interval
    # (we use a local throwaway scheduler so we don't mutate global state)
    local_scheduler = AsyncIOScheduler()
    local_scheduler.add_job(
        budget_guardrails_job,
        "interval",
        hours=settings.BUDGET_GUARDRAILS_INTERVAL_HOURS,
        id="budget_guardrails_test",
        replace_existing=True,
    )
    job_ids = [j.id for j in local_scheduler.get_jobs()]
    assert "budget_guardrails_test" in job_ids

    # Confirm configured interval matches the setting
    job = local_scheduler.get_job("budget_guardrails_test")
    assert job.trigger.interval.total_seconds() == settings.BUDGET_GUARDRAILS_INTERVAL_HOURS * 3600

    # Clean up without starting the scheduler (avoids event-loop side-effects)
    local_scheduler.remove_all_jobs()


@pytest.mark.asyncio
async def test_non_idempotent_scheduler_jobs_blocked_by_default():
    """Non-idempotent jobs should not be registerable without explicit opt-in."""
    original_value = settings.SCHEDULER_ALLOW_NON_IDEMPOTENT_JOBS
    settings.SCHEDULER_ALLOW_NON_IDEMPOTENT_JOBS = False
    try:
        with pytest.raises(RuntimeError):
            register_interval_job(
                budget_guardrails_job,  # function value is irrelevant for guard check
                job_id="non_idempotent_test_job",
                hours=1,
                idempotent=False,
            )
    finally:
        settings.SCHEDULER_ALLOW_NON_IDEMPOTENT_JOBS = original_value


# ---------------------------------------------------------------------------
# Main workflow E2E — warning → idempotency → critical → auto-resolve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_guardrails_workflow_e2e(override_database_url):
    """
    Test the full budget guardrails state machine for a single workspace:
      A. Under threshold → no todo created
      B. Warning threshold breach (95% spend, warning fires at ≥90%) → todo created
      C. Idempotency — same state, no duplicates
      D. Critical threshold breach (105% spend) → existing todo escalated
      E. Spend removed → todo auto-resolved
    """
    workspace_id = 501
    today = datetime.now(UTC).date()
    month_start = today.replace(day=1)

    async with postgres.async_session_maker() as session:
        cat_id = await _seed_category_budget_transaction(
            session,
            workspace_id,
            user_id=1,
            cat_name="Groceries",
            budget_amount=100.00,
            spend_amount=0.0,
            month_start=month_start,
        )
        await session.commit()

    # --- Case A: Under threshold (50% spend) → no todo ---
    async with postgres.async_session_maker() as session:
        tx = SpendingTransaction(
            workspace_id=workspace_id,
            user_id=1,
            category_id=cat_id,
            amount=50.00,
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
        assert len(todos) == 0, "No todo should be created under threshold"

    # --- Case B: Warning threshold breach (95% spend, threshold ≥90%) → todo created ---
    async with postgres.async_session_maker() as session:
        tx2 = SpendingTransaction(
            workspace_id=workspace_id,
            user_id=1,
            category_id=cat_id,
            amount=45.00,  # 50 + 45 = 95 → 95%
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
        assert not todo.completed
        todo_uuid = todo.public_id

        # Audit log must record the creation
        audits = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.workspace_id == workspace_id)
                    .where(AuditLog.action == "budget_guardrail_triggered")
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 1
        assert audits[0].details["entity_public_id"] == str(todo_uuid)
        assert audits[0].details["before"] is None
        assert "[Budget] Warning" in audits[0].details["after"]["title"]

    # --- Case C: Idempotency — re-run at the same 95% spend → no new todo, no new audit ---
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
        assert len(todos) == 1, "Idempotency: no duplicate todo should be created"

        audits = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.workspace_id == workspace_id)
                    .where(AuditLog.action == "budget_guardrail_triggered")
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 1, "Idempotency: no duplicate audit log should be created"

    # --- Case D: Critical threshold breach (105% spend) → todo escalated ---
    async with postgres.async_session_maker() as session:
        tx3 = SpendingTransaction(
            workspace_id=workspace_id,
            user_id=1,
            category_id=cat_id,
            amount=10.00,  # 95 + 10 = 105 → 105%
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
        assert len(todos) == 1, "Still only one todo — updated in place"
        assert "[Budget] Critical" in todos[0].title

        audits = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.workspace_id == workspace_id)
                    .where(AuditLog.action == "budget_guardrail_triggered")
                    .order_by(AuditLog.timestamp.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 2
        # Second audit records the escalation from Warning → Critical
        assert "[Budget] Warning" in audits[1].details["before"]["title"]
        assert "[Budget] Critical" in audits[1].details["after"]["title"]

    # --- Case E: Spend cleared → todo auto-resolved ---
    async with postgres.async_session_maker() as session:
        await session.execute(
            sa.delete(SpendingTransaction).where(SpendingTransaction.workspace_id == workspace_id)
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
        assert todos[0].completed, "Todo should be auto-resolved when spend drops below threshold"

        audits = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.workspace_id == workspace_id)
                    .where(AuditLog.action == "budget_guardrail_triggered")
                    .order_by(AuditLog.timestamp.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 3
        assert audits[2].details["before"]["completed"] is False
        assert audits[2].details["after"]["completed"] is True


# ---------------------------------------------------------------------------
# Fix 5: Cross-workspace isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_guardrails_cross_workspace_isolation(override_database_url):
    """
    Spec 009 §7 — cross-workspace isolation.

    Running guardrails for workspace A (which has a budget breach) must NOT
    create todos in workspace B (which has no breach).
    """
    today = datetime.now(UTC).date()
    month_start = today.replace(day=1)

    # Seed workspace B (502) with a budget that does NOT breach
    async with postgres.async_session_maker() as session:
        await _seed_workspace(
            session,
            workspace_id=502,
            user_id=2,
            workspace_name="Isolated Workspace B",
            email="ws_b@example.com",
            username="ws_b_user",
        )
        await session.commit()

    # Workspace A (501): budget = 100, spend = 95 → 95% → warning breach
    async with postgres.async_session_maker() as session:
        await _seed_category_budget_transaction(
            session,
            workspace_id=501,
            user_id=1,
            cat_name="A Groceries",
            budget_amount=100.00,
            spend_amount=95.00,
            month_start=month_start,
        )
        await session.commit()

    # Workspace B (502): budget = 100, spend = 30 → 30% → no breach
    async with postgres.async_session_maker() as session:
        await _seed_category_budget_transaction(
            session,
            workspace_id=502,
            user_id=2,
            cat_name="B Groceries",
            budget_amount=100.00,
            spend_amount=30.00,
            month_start=month_start,
        )
        await session.commit()

    # Run guardrails for workspace A only
    async with postgres.async_session_maker() as session:
        ws_a = (await session.execute(select(Workspace).where(Workspace.id == 501))).scalar_one()
        await evaluate_workspace_budget_guardrails(session, ws_a)
        await session.commit()

    # Workspace A: one warning todo created
    async with postgres.async_session_maker() as session:
        a_todos = (
            (await session.execute(select(Todo).where(Todo.workspace_id == 501))).scalars().all()
        )
        assert len(a_todos) == 1
        assert "[Budget] Warning" in a_todos[0].title

    # Workspace B: zero todos — breach in A must not bleed across
    async with postgres.async_session_maker() as session:
        b_todos = (
            (await session.execute(select(Todo).where(Todo.workspace_id == 502))).scalars().all()
        )
        assert len(b_todos) == 0, (
            "Cross-workspace isolation failed: workspace B must have no todos "
            "after only workspace A was evaluated"
        )


# ---------------------------------------------------------------------------
# Fix 6: Per-workspace failure isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_guardrails_per_workspace_failure_isolation(override_database_url):
    """
    Spec 009 §7 — per-workspace failure isolation.

    If evaluating workspace A throws an unhandled exception, the job must catch
    it, log it, and continue evaluating workspace B.  Workspace B's guardrail
    todo must still be created.
    """
    today = datetime.now(UTC).date()
    month_start = today.replace(day=1)

    # Seed workspace B (502) with a budget breach so we can verify it completes
    async with postgres.async_session_maker() as session:
        await _seed_workspace(
            session,
            workspace_id=502,
            user_id=2,
            workspace_name="Workspace B (failure isolation)",
            email="ws_b_fail@example.com",
            username="ws_b_fail",
        )
        await session.commit()

    async with postgres.async_session_maker() as session:
        await _seed_category_budget_transaction(
            session,
            workspace_id=502,
            user_id=2,
            cat_name="B Rent",
            budget_amount=100.00,
            spend_amount=95.00,
            month_start=month_start,
        )
        await session.commit()

    # Patch the workflow in jobs module so workspace 501 raises, 502 runs normally
    real_fn = evaluate_workspace_budget_guardrails

    async def selective_fail(session, workspace):
        if workspace.id == 501:
            raise RuntimeError("Simulated evaluation failure for workspace 501")
        return await real_fn(session, workspace)

    with patch(
        "app.application.jobs.evaluate_workspace_budget_guardrails",
        side_effect=selective_fail,
    ):
        await budget_guardrails_job()

    # Workspace 501: evaluation failed → no todos
    async with postgres.async_session_maker() as session:
        a_todos = (
            (await session.execute(select(Todo).where(Todo.workspace_id == 501))).scalars().all()
        )
        assert len(a_todos) == 0, "Workspace 501 failed — it should have no todos"

    # Workspace 502: completed despite workspace 501's failure → todo created
    async with postgres.async_session_maker() as session:
        b_todos = (
            (await session.execute(select(Todo).where(Todo.workspace_id == 502))).scalars().all()
        )
        assert len(b_todos) == 1, (
            "Workspace 502 must still get its guardrail todo despite workspace 501 failing"
        )
        assert "[Budget] Warning" in b_todos[0].title
