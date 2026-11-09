"""Regression tests for defects found in the weekly review (hardening PR).

Real Postgres through the MCP tool functions; only ``authorize_workspace`` is patched.
Each test pins one defect that was reproduced against the shipped code first.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.auth.models import User
from app.config import settings
from app.core.database import postgres
from app.finance.models import Account
from app.investing.models import Instrument, InstrumentConstituent, InvestingOrder
from app.mcp.server import create_mcp_server
from app.platform.models import Workspace, WorkspaceMembership

WS = 8301
OTHER_WS = 8302
ALICE = 8301
BOB = 8302


@pytest.fixture(autouse=True)
def _mcp_settings(monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.lifestack.test")


@pytest.fixture
def authorize(monkeypatch):
    mock = AsyncMock(return_value=ALICE)
    monkeypatch.setattr("app.mcp.security.authorize_workspace", mock)
    return mock


async def _tools():
    return {t.name: t for t in await create_mcp_server().list_tools()}


async def _seed() -> dict:
    async with postgres.async_session_maker() as session, session.begin():
        session.add_all([
            User(id=ALICE, email="a-hard@example.com", username="a_hard", hashed_password="x"),
            User(id=BOB, email="b-hard@example.com", username="b_hard", hashed_password="x"),
            Workspace(id=WS, name="Hardening"),
            Workspace(id=OTHER_WS, name="Other"),
        ])
        await session.flush()
        session.add_all([
            WorkspaceMembership(workspace_id=WS, user_id=ALICE, role="owner"),
            WorkspaceMembership(workspace_id=OTHER_WS, user_id=BOB, role="owner"),
        ])
        global_etf = Instrument(
            workspace_id=None, symbol="GLOBETF", name="Shared Global ETF", instrument_type="etf"
        )
        own_etf = Instrument(
            workspace_id=WS, symbol="OWNETF", name="Workspace ETF", instrument_type="etf"
        )
        session.add_all([global_etf, own_etf])
        await session.flush()
        return {"global": str(global_etf.public_id), "own": str(own_etf.public_id)}


def _snapshot_args(instrument: str, **over) -> dict:
    args = {
        "workspace_id": WS,
        "instrument_public_id": instrument,
        "as_of_date": "2026-11-01",
        "source": "issuer",
        "fetched_at": "2026-11-02T00:00:00+00:00",
        "constituents": [
            {"company_name": "Apple Inc", "company_ticker": "AAPL", "weight": "0.6"},
            {"company_name": "Microsoft", "company_ticker": "MSFT", "weight": "0.4"},
        ],
        "confirmed": True,
    }
    args.update(over)
    return args


async def _constituent_rows() -> int:
    async with postgres.async_session_maker() as session:
        return (
            await session.execute(select(func.count()).select_from(InstrumentConstituent))
        ).scalar_one()


# --------------------------------------------------------------------------
# get_allocation_vs_target / get_lookthrough_exposure
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allocation_vs_target_default_call_does_not_crash(override_database_url, authorize):
    await _seed()
    tools = await _tools()

    res = await tools["get_allocation_vs_target"].fn(workspace_id=WS)

    assert res["status"] == "success", res


@pytest.mark.asyncio
async def test_allocation_vs_target_new_cash_default_is_zero_in_the_schema(
    override_database_url, authorize
):
    tools = await _tools()
    schema = tools["get_allocation_vs_target"].parameters["properties"]["new_cash"]
    assert schema.get("default") == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["-5", "NaN", "Infinity", "1.234", "abc"])
async def test_allocation_vs_target_rejects_bad_cash_with_an_error_not_an_exception(
    override_database_url, authorize, bad
):
    await _seed()
    tools = await _tools()

    res = await tools["get_allocation_vs_target"].fn(workspace_id=WS, new_cash=bad)

    assert res["status"] == "error", (bad, res)


@pytest.mark.asyncio
async def test_lookthrough_exposure_default_returns_every_grouping(
    override_database_url, authorize
):
    await _seed()
    tools = await _tools()

    res = await tools["get_lookthrough_exposure"].fn(workspace_id=WS)

    assert res["status"] == "success", res
    for key in ("by_company", "by_sector", "by_country", "by_theme"):
        assert res[key] is not None, f"omitting group_by must return all groupings ({key})"


@pytest.mark.asyncio
async def test_lookthrough_exposure_group_by_is_an_enum_in_the_schema(
    override_database_url, authorize
):
    tools = await _tools()
    schema = tools["get_lookthrough_exposure"].parameters["properties"]["group_by"]
    allowed = {"company", "sector", "country", "theme"}
    values = set(schema.get("enum", []))
    for branch in schema.get("anyOf", []):
        values |= set(branch.get("enum", []))
    assert values == allowed, schema


@pytest.mark.asyncio
async def test_lookthrough_exposure_single_group_by_still_limits_the_response(
    override_database_url, authorize
):
    await _seed()
    tools = await _tools()

    res = await tools["get_lookthrough_exposure"].fn(workspace_id=WS, group_by="sector")

    assert res["status"] == "success"
    assert res["by_sector"] is not None
    assert res["by_company"] is None and res["by_country"] is None and res["by_theme"] is None


# --------------------------------------------------------------------------
# A workspace can still write and delete constituents of its own fund
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_workspace_can_still_write_and_delete_its_own_funds_snapshot(
    override_database_url, authorize
):
    ids = await _seed()
    tools = await _tools()

    written = await tools["write_investment_constituent_snapshot"].fn(**_snapshot_args(ids["own"]))
    assert written["status"] == "success", written
    assert written["total"] == 2
    assert await _constituent_rows() == 2

    deleted = await tools["delete_investment_constituent_snapshot"].fn(
        workspace_id=WS,
        instrument_public_id=ids["own"],
        as_of_date="2026-11-01",
        source="issuer",
        confirmed=True,
    )
    assert deleted["status"] == "success", deleted
    assert await _constituent_rows() == 0


# --------------------------------------------------------------------------
# update_investing_account_status must persist what it reports
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_investing_account_status_persists_scope_and_exchange(
    override_database_url, authorize
):
    await _seed()
    async with postgres.async_session_maker() as session, session.begin():
        account = Account(
            workspace_id=WS,
            name="Tax Broker",
            account_type="brokerage",
            default_currency_code="INR",
        )
        session.add(account)
        await session.flush()
        instrument = Instrument(workspace_id=WS, symbol="INFY", name="Infosys")
        session.add(instrument)
        await session.flush()
        session.add(
            InvestingOrder(
                workspace_id=WS,
                user_id=ALICE,
                account_id=account.id,
                order_type="buy",
                symbol="INFY",
                instrument_id=instrument.id,
                quantity=Decimal("1"),
                price_per_unit=Decimal("100"),
                gross_amount=Decimal("100"),
                net_amount=Decimal("100"),
                currency="INR",
                occurred_at=datetime(2026, 1, 5, tzinfo=UTC),
            )
        )
        account_public, account_pk, instrument_pk = account.public_id, account.id, instrument.id
    tools = await _tools()

    res = await tools["update_investing_account_status"].fn(
        workspace_id=WS,
        account_id=str(account_public),
        market_scope="foreign",
        exchange_name="NSE",
        confirmed=True,
    )

    assert res["status"] == "success", res
    assert [i["symbol"] for i in res["instruments_updated"]] == ["INFY"]
    # Read back in a brand-new session: the tool said it wrote, so it must have committed.
    async with postgres.async_session_maker() as session:
        stored_account = await session.get(Account, account_pk)
        stored_instrument = await session.get(Instrument, instrument_pk)
    assert stored_account.market_scope == "foreign"
    assert stored_instrument.exchange == "NSE"


# --------------------------------------------------------------------------
# Tax-profile writers must persist, and set_fx_rate must say what it set
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_fx_rate_persists_and_the_read_tool_sees_it(override_database_url, authorize):
    await _seed()
    tools = await _tools()

    res = await tools["set_fx_rate"].fn(
        workspace_id=WS, date_str="2026-10-31", currency="usd", rate="83.5", confirmed=True
    )

    assert res["status"] == "success", res
    assert "2026-10-31" in res["message"] and "class" not in res["message"], res["message"]
    # The round trip the LRS/tax tools depend on: a *separate* session must see the rate.
    read = await tools["get_fx_rate"].fn(workspace_id=WS, date_str="2026-10-31", currency="USD")
    assert read["status"] == "success", read
    assert read["rate"] == "83.5"


@pytest.mark.asyncio
async def test_update_tax_profile_persists(override_database_url, authorize):
    await _seed()
    tools = await _tools()

    res = await tools["update_tax_profile"].fn(
        workspace_id=WS, notes="reviewed 2026", confirmed=True
    )

    assert res["notes"] == "reviewed 2026", res
    again = await tools["get_tax_profile"].fn(workspace_id=WS)
    assert again["notes"] == "reviewed 2026", "the update was reported but never committed"
