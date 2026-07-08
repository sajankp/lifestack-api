"""Integration tests for Net Worth snapshots, live cash, and history endpoints."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.application.jobs import net_worth_snapshot_job
from app.core.database import postgres
from app.finance.models import NetWorthSnapshot
from app.investing.models import PortfolioSnapshot
from app.platform.models import Workspace


async def _register_and_login(client: AsyncClient, email: str, username: str) -> dict:
    register_res = await client.post(
        "/v1/auth/register",
        json={"email": email, "username": username, "password": "TestPass123!"},
    )
    assert register_res.status_code == 200
    login_res = await client.post(
        "/v1/auth/login",
        data={"username": username, "password": "TestPass123!"},
    )
    assert login_res.status_code == 200
    return dict(login_res.cookies)


async def _create_account(client, cookies, name, currency="USD"):
    res = await client.post(
        "/v1/finance/accounts",
        json={"name": name, "account_type": "wallet", "default_currency_code": currency},
        cookies=cookies,
    )
    assert res.status_code == 201, res.text
    return res.json()["public_id"]


@pytest.mark.asyncio
async def test_net_worth_live_cash_and_snapshot_creation(client: AsyncClient):
    """Test that fetching net-worth opportunistically creates a daily snapshot."""
    cookies = await _register_and_login(client, "nw_snap@example.com", "nw_snap")

    # Fetch net worth (initially empty status because there are no accounts/data)
    res = await client.get("/v1/finance/net-worth", cookies=cookies)
    assert res.status_code == 200
    data = res.json()
    assert data["valuation_status"] == "empty"

    # Create a spending account
    await _create_account(client, cookies, "Test Wallet")

    # Fetch net worth (now no reporting currency is set)
    res = await client.get("/v1/finance/net-worth", cookies=cookies)
    assert res.status_code == 200
    data = res.json()
    assert data["valuation_status"] == "no_reporting_currency"

    # Set reporting currency to USD in finance settings
    settings_res = await client.patch(
        "/v1/finance/settings", json={"reporting_currency_code": "USD"}, cookies=cookies
    )
    assert settings_res.status_code == 200

    # Fetch net worth again
    res = await client.get("/v1/finance/net-worth", cookies=cookies)
    assert res.status_code == 200
    data = res.json()
    assert data["valuation_status"] == "ok"
    assert data["reporting_currency"] == "USD"
    assert float(data["total_net_worth"]) == 0.0

    # Verify that an opportunistic snapshot was created for today
    today_date = datetime.now(UTC).date()
    session_maker = postgres.async_session_maker
    async with session_maker() as session:
        stmt = select(NetWorthSnapshot).where(NetWorthSnapshot.snapshot_date == today_date)
        db_res = await session.execute(stmt)
        snapshots = db_res.scalars().all()
        # At least one snapshot should exist
        assert len(snapshots) >= 1
        user_snapshot = [s for s in snapshots if s.reporting_currency == "USD"][0]
        assert user_snapshot.reporting_currency == "USD"
        assert user_snapshot.total_net_worth == Decimal("0.00")

    # Verify history endpoint returns the snapshot
    history_res = await client.get("/v1/finance/net-worth/history", cookies=cookies)
    assert history_res.status_code == 200
    history_data = history_res.json()
    assert len(history_data) >= 1
    assert history_data[0]["reporting_currency"] == "USD"
    assert float(history_data[0]["total_net_worth"]) == 0.0


@pytest.mark.asyncio
async def test_net_worth_snapshot_job(client: AsyncClient):
    """Test that running the daily snapshot job creates/updates the snapshot."""
    cookies = await _register_and_login(client, "nw_job@example.com", "nw_job")

    # Create account
    await _create_account(client, cookies, "Test Wallet")

    # Set reporting currency
    await client.patch(
        "/v1/finance/settings", json={"reporting_currency_code": "USD"}, cookies=cookies
    )

    # Insert a dummy PortfolioSnapshot for the workspace so the job is not skipped
    today_date = datetime.now(UTC).date()
    session_maker = postgres.async_session_maker
    async with session_maker() as session:
        db_res = await session.execute(select(Workspace))
        workspace = db_res.scalars().first()
        assert workspace is not None
        workspace_id = workspace.id

        portfolio_snapshot = PortfolioSnapshot(
            workspace_id=workspace_id,
            snapshot_date=today_date,
            total_value=Decimal("0.00"),
            total_cost=Decimal("0.00"),
            holdings_value=Decimal("0.00"),
            cash_value=Decimal("0.00"),
            currency_code="USD",
            fx_rates_used={},
        )
        session.add(portfolio_snapshot)
        await session.commit()

    # Run daily net worth snapshot job
    await net_worth_snapshot_job()

    # Check if a snapshot row was created for today
    async with session_maker() as session:
        stmt = select(NetWorthSnapshot).where(NetWorthSnapshot.snapshot_date == today_date)
        db_res = await session.execute(stmt)
        snapshots = db_res.scalars().all()
        assert len(snapshots) >= 1
