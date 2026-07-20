"""spec-088 retry + job_failures ledger wiring tests (testing plan items 5-8).

Covers: per-workspace retry exhaustion writes a scoped ledger row while other
workspaces still succeed (isolation preserved), a later success auto-resolves
the row, and the two global (non-run_workspace_job) opted-in jobs
(fx_rate_ingestion_job, bhavcopy_price_feed_job) write a workspace_id=NULL row
on exhaustion. test_scheduler.py's own single-connection assertions (item 8)
are unchanged and covered there.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.application.jobs import (
    bhavcopy_price_feed_job,
    fx_rate_ingestion_job,
    investment_closing_prices_job,
)
from app.auth.models import User
from app.config import settings
from app.core.database import postgres
from app.core.job_failures import JobFailure
from app.investing import service as investing_service
from app.investing.performance_service import PerformanceService
from app.platform.models import Workspace, WorkspaceMembership


@pytest.fixture(autouse=True)
async def fast_retry_settings(monkeypatch):
    """Deterministic, fast retries -- no real backoff delay in tests."""
    monkeypatch.setattr(settings, "JOB_RETRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(settings, "JOB_RETRY_BASE_DELAY_SECONDS", 0.0)


@pytest.fixture(autouse=True)
async def seed_two_workspaces(override_database_url):
    async with postgres.async_session_maker() as session:
        session.add(
            User(
                id=1,
                email="retry_actor@example.com",
                username="retry_actor",
                hashed_password="hashed_password_here",
            )
        )
        for wid in (701, 702):
            session.add(Workspace(id=wid, name=f"Workspace {wid}"))
            await session.flush()
            session.add(WorkspaceMembership(workspace_id=wid, user_id=1, role="owner"))
        await session.commit()


@pytest.mark.asyncio
async def test_investment_closing_prices_job_records_failure_and_isolates_workspaces(
    override_database_url,
):
    async def side_effect(workspace_id: int):
        if workspace_id == 701:
            raise httpx.ConnectError("feed unreachable")
        return {}

    refresh_mock = AsyncMock(side_effect=side_effect)
    with patch.object(PerformanceService, "refresh_workspace_prices", new=refresh_mock):
        await investment_closing_prices_job()

    # 701 retried JOB_RETRY_MAX_ATTEMPTS (2) times; 702 processed once and succeeded.
    calls_701 = [c for c in refresh_mock.await_args_list if c.args[0] == 701]
    calls_702 = [c for c in refresh_mock.await_args_list if c.args[0] == 702]
    assert len(calls_701) == 2
    assert len(calls_702) == 1

    async with postgres.async_session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(JobFailure).where(JobFailure.job_name == "investment_closing_prices_job")
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 1
    assert rows[0].workspace_id == 701
    assert rows[0].attempts == 2
    assert rows[0].error_type == "ConnectError"
    assert rows[0].resolved_at is None


@pytest.mark.asyncio
async def test_investment_closing_prices_job_auto_resolves_after_later_success(
    override_database_url,
):
    failing_mock = AsyncMock(side_effect=httpx.ConnectError("feed unreachable"))
    with patch.object(PerformanceService, "refresh_workspace_prices", new=failing_mock):
        await investment_closing_prices_job(workspace_id=701)

    async with postgres.async_session_maker() as session:
        open_rows = (
            (
                await session.execute(
                    select(JobFailure).where(JobFailure.job_name == "investment_closing_prices_job")
                )
            )
            .scalars()
            .all()
        )
    assert len(open_rows) == 1
    assert open_rows[0].resolved_at is None

    succeeding_mock = AsyncMock(return_value={})
    with patch.object(PerformanceService, "refresh_workspace_prices", new=succeeding_mock):
        await investment_closing_prices_job(workspace_id=701)

    async with postgres.async_session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(JobFailure).where(JobFailure.job_name == "investment_closing_prices_job")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].resolved_at is not None


@pytest.mark.asyncio
async def test_fx_rate_ingestion_job_records_global_failure_on_exhaustion(
    override_database_url, monkeypatch
):
    monkeypatch.setattr(settings, "EXCHANGERATE_API_KEY", "test-key")

    with (
        patch.object(
            httpx.AsyncClient, "get", new=AsyncMock(side_effect=httpx.ConnectError("api down"))
        ),
        pytest.raises(httpx.ConnectError),
    ):
        await fx_rate_ingestion_job()

    async with postgres.async_session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(JobFailure).where(JobFailure.job_name == "fx_rate_ingestion_job")
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 1
    assert rows[0].workspace_id is None
    assert rows[0].attempts == 2
    assert rows[0].error_type == "ConnectError"


@pytest.mark.asyncio
async def test_bhavcopy_price_feed_job_records_global_failure_and_does_not_raise(
    override_database_url, monkeypatch
):
    monkeypatch.setattr(
        investing_service,
        "_fetch_nse_bhavcopy",
        AsyncMock(side_effect=httpx.ConnectError("nse unreachable")),
    )

    await bhavcopy_price_feed_job()

    async with postgres.async_session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(JobFailure).where(JobFailure.job_name == "bhavcopy_price_feed_job")
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 1
    assert rows[0].workspace_id is None
    assert rows[0].attempts == 2
