"""Integration tests for Net Worth snapshots, live cash, and history endpoints."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.application.jobs import net_worth_snapshot_job
from app.auth.models import User
from app.core.audit import AuditLog
from app.core.database import postgres
from app.finance.models import Currency, NetWorthSnapshot, WorkspaceCurrency
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
        ws_res = await session.execute(
            select(Workspace).where(Workspace.name == "nw_snap's Workspace")
        )
        workspace = ws_res.scalar_one()

        stmt = select(NetWorthSnapshot).where(
            NetWorthSnapshot.snapshot_date == today_date,
            NetWorthSnapshot.workspace_id == workspace.id,
        )
        db_res = await session.execute(stmt)
        user_snapshot = db_res.scalar_one()
        assert user_snapshot.reporting_currency == "USD"
        assert user_snapshot.total_net_worth == Decimal("0.00")

    # Verify history endpoint returns the snapshot
    history_res = await client.get("/v1/finance/net-worth/history", cookies=cookies)
    assert history_res.status_code == 200
    history_data = history_res.json()
    assert len(history_data) >= 1
    assert history_data[0]["reporting_currency"] == "USD"
    assert float(history_data[0]["total_net_worth"]) == 0.0
    # spec-086 Layer 3: no reverted import for this workspace -> not flagged.
    assert history_data[0]["data_revised"] is False


async def _create_category(client, cookies, name="General"):
    res = await client.post(
        "/v1/spending/categories",
        json={"name": name, "color": "#00FF00", "icon": "🏦"},
        cookies=cookies,
    )
    assert res.status_code == 201, res.text
    return res.json()["public_id"]


async def _add_income(client, cookies, account_id, category_id, amount):
    res = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": category_id,
            "account_id": account_id,
            "amount": amount,
            "type": "income",
            "occurred_at": datetime.now(UTC).isoformat(),
            "description": "seed balance",
        },
        cookies=cookies,
    )
    assert res.status_code == 201, res.text


@pytest.mark.asyncio
async def test_net_worth_partial_when_one_currency_unconvertible(client: AsyncClient):
    """spec-091 / issue #182: one spending currency lacking an FX rate must
    not blank the whole headline. total_net_worth/spending_total stay null
    (unchanged contract), but the new *_partial fields expose the sum of
    convertible balances plus which currencies were excluded."""
    cookies = await _register_and_login(client, "nw_partial@example.com", "nw_partial")
    await client.patch(
        "/v1/finance/settings", json={"reporting_currency_code": "USD"}, cookies=cookies
    )
    category_id = await _create_category(client, cookies)

    session_maker = postgres.async_session_maker
    async with session_maker() as session:
        ws_res = await session.execute(
            select(Workspace).where(Workspace.name == "nw_partial's Workspace")
        )
        workspace_id = ws_res.scalar_one().id
        existing_eur = (
            await session.execute(select(Currency).where(Currency.code == "EUR"))
        ).scalar_one_or_none()
        if existing_eur is None:
            session.add(Currency(code="EUR", name="Euro", symbol="EUR", minor_unit=2))
            await session.flush()
        session.add(WorkspaceCurrency(workspace_id=workspace_id, currency_code="EUR"))
        await session.commit()

    usd_account = await _create_account(client, cookies, "USD Wallet", "USD")
    eur_account = await _create_account(client, cookies, "EUR Wallet", "EUR")
    await _add_income(client, cookies, usd_account, category_id, "500.00")
    await _add_income(client, cookies, eur_account, category_id, "300.00")
    # No EUR->USD (or USD->EUR) FxRate seeded -- EUR stays unconvertible.

    res = await client.get("/v1/finance/net-worth", cookies=cookies)
    assert res.status_code == 200
    data = res.json()

    assert data["valuation_status"] == "partial"
    assert data["total_net_worth"] is None
    assert data["spending_total"] is None
    assert float(data["spending_total_partial"]) == 500.0
    assert data["excluded_currencies"] == [{"currency_code": "EUR", "total_balance": "300.00"}]

    # No snapshot persisted from a partial valuation (unchanged, no double record).
    session_maker = postgres.async_session_maker
    async with session_maker() as session:
        ws_res = await session.execute(
            select(Workspace).where(Workspace.name == "nw_partial's Workspace")
        )
        workspace = ws_res.scalar_one()
        snap_res = await session.execute(
            select(NetWorthSnapshot).where(NetWorthSnapshot.workspace_id == workspace.id)
        )
        assert snap_res.scalars().first() is None


@pytest.mark.asyncio
async def test_net_worth_ok_status_has_no_partial_fields(client: AsyncClient):
    """Regression: when every currency converts, the new fields stay empty
    and the existing total_net_worth/spending_total are untouched."""
    cookies = await _register_and_login(client, "nw_full@example.com", "nw_full")
    await client.patch(
        "/v1/finance/settings", json={"reporting_currency_code": "USD"}, cookies=cookies
    )
    category_id = await _create_category(client, cookies)
    usd_account = await _create_account(client, cookies, "USD Wallet", "USD")
    await _add_income(client, cookies, usd_account, category_id, "500.00")

    res = await client.get("/v1/finance/net-worth", cookies=cookies)
    assert res.status_code == 200
    data = res.json()

    assert data["valuation_status"] == "ok"
    assert float(data["total_net_worth"]) == 500.0
    assert data["spending_total_partial"] is None
    assert data["total_net_worth_partial"] is None
    assert data["excluded_currencies"] == []


@pytest.mark.asyncio
async def test_net_worth_history_flags_point_overlapping_reverted_import(client: AsyncClient):
    """spec-086 Layer 3: a history point dated inside a since-reverted
    import's live window must be flagged data_revised=True -- the daily
    equivalent of the weekly-summary data_revised_after_snapshot signal,
    sourced from the same append-only import_rolled_back audit trail."""
    cookies = await _register_and_login(client, "nw_snap_revert@example.com", "nw_snap_revert")
    settings_res = await client.patch(
        "/v1/finance/settings", json={"reporting_currency_code": "USD"}, cookies=cookies
    )
    assert settings_res.status_code == 200

    today_date = datetime.now(UTC).date()
    session_maker = postgres.async_session_maker
    async with session_maker() as session:
        ws_res = await session.execute(
            select(Workspace).where(Workspace.name == "nw_snap_revert's Workspace")
        )
        workspace = ws_res.scalar_one()
        user = (
            await session.execute(select(User).where(User.username == "nw_snap_revert"))
        ).scalar_one()

        session.add(
            NetWorthSnapshot(
                workspace_id=workspace.id,
                snapshot_date=today_date - timedelta(days=3),
                reporting_currency="USD",
                total_net_worth=Decimal("5000.00"),
                source="user_provided",
            )
        )
        # An import live 4 days ago, reverted 2 days ago -- covers the
        # snapshot dated 3 days ago, but not today's.
        session.add(
            AuditLog(
                workspace_id=workspace.id,
                actor_id=user.id,
                action="import_rolled_back",
                module="import",
                entity_type="import_batch",
                entity_id=999999,
                details={
                    "entity_public_id": str(uuid.uuid4()),
                    "before": {
                        "module": "investing_orders",
                        "status": "completed",
                        "total_rows": 1,
                        "valid_rows": 1,
                        "error_rows": 0,
                        "committed_at": datetime.combine(
                            today_date - timedelta(days=4), datetime.min.time(), tzinfo=UTC
                        ).isoformat(),
                    },
                    "after": None,
                    "changed_fields": ["status"],
                },
                timestamp=datetime.combine(
                    today_date - timedelta(days=2), datetime.min.time(), tzinfo=UTC
                ),
            )
        )
        await session.commit()

    history_res = await client.get(
        "/v1/finance/net-worth/history",
        params={
            "from_date": (today_date - timedelta(days=10)).isoformat(),
            "to_date": today_date.isoformat(),
        },
        cookies=cookies,
    )
    assert history_res.status_code == 200, history_res.text
    by_date = {row["snapshot_date"]: row for row in history_res.json()}
    flagged_date = (today_date - timedelta(days=3)).isoformat()
    assert by_date[flagged_date]["data_revised"] is True


@pytest.mark.asyncio
async def test_net_worth_snapshot_job(client: AsyncClient):
    """Test that running the daily snapshot job creates/updates the snapshot.

    Deliberately does NOT create a PortfolioSnapshot row: the job must compute
    holdings value live via the summary service, not depend on an on-demand
    PortfolioSnapshot existing (regression test for the job silently skipping
    workspaces that never visited the investing dashboard).
    """
    cookies = await _register_and_login(client, "nw_job@example.com", "nw_job")

    # Create account
    await _create_account(client, cookies, "Test Wallet")

    # Set reporting currency
    await client.patch(
        "/v1/finance/settings", json={"reporting_currency_code": "USD"}, cookies=cookies
    )

    session_maker = postgres.async_session_maker
    async with session_maker() as session:
        db_res = await session.execute(
            select(Workspace).where(Workspace.name == "nw_job's Workspace")
        )
        workspace = db_res.scalar_one()
        workspace_id = workspace.id

    # Run daily net worth snapshot job
    await net_worth_snapshot_job()

    # Check if a snapshot row was created for today, for this workspace
    today_date = datetime.now(UTC).date()
    async with session_maker() as session:
        stmt = select(NetWorthSnapshot).where(
            NetWorthSnapshot.snapshot_date == today_date,
            NetWorthSnapshot.workspace_id == workspace_id,
        )
        db_res = await session.execute(stmt)
        snapshot = db_res.scalar_one()
        assert snapshot.total_net_worth == Decimal("0.00")
