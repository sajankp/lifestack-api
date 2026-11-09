"""Company sector writes, forking and look-through rules, DB-backed (spec-100).

Drives the real ``set_company_sector`` / ``get_investment_sector_allocation`` /
``list_investment_holdings`` MCP tools against the transactional test DB.
"""

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.auth.repository import UserRepository
from app.config import settings
from app.core.database import postgres
from app.investing.company_sector_service import SectorRowError
from app.investing.models import Company, Holding, Instrument, InstrumentConstituent
from app.investing.repository import CompanyRepository
from app.mcp.server import create_mcp_server
from app.platform.models import Workspace
from app.tests.integration.test_investing import (
    _register_and_login,
    _submit_holding_price,
)
from app.tests.investing.test_sector_allocation import (
    _buy,
    _constituent,
    _create_instrument,
    _write_snapshot,
)


@pytest.fixture(autouse=True)
def _no_network_price_lookup(monkeypatch):
    """Keep instruments/companies workspace-scoped (no live price lookup)."""

    async def _none(*args, **kwargs):
        return None

    monkeypatch.setattr("app.investing.service._fetch_stock_price", _none)


@pytest.fixture
async def tools(monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.lifestack.test")
    server = create_mcp_server()
    tool_map = {t.name: t.fn for t in await server.list_tools()}
    monkeypatch.setattr("app.mcp.server.authorize_workspace", AsyncMock(return_value=1))
    return tool_map


async def _workspace_ids() -> list[int]:
    async with postgres.async_session_maker() as session:
        rows = (await session.execute(select(Workspace.id).order_by(Workspace.id))).all()
        return [r[0] for r in rows]


async def _registered_user_id(session, email: str) -> int:
    user = await UserRepository(session).get_by_email(email)
    assert user is not None and user.id is not None, f"no user with email {email}"
    return user.id


async def _holding_company(holding_public_id: str) -> tuple[Holding, Instrument, Company]:
    async with postgres.async_session_maker() as session:
        holding = (
            await session.execute(
                select(Holding).where(Holding.public_id == uuid.UUID(holding_public_id))
            )
        ).scalar_one()
        instrument = await session.get(Instrument, holding.instrument_id)
        company = await session.get(Company, instrument.company_id)
        return holding, instrument, company


async def _register(client: AsyncClient, name: str) -> str:
    accounts = await _register_and_login(
        client, email=f"{name}@example.com", username=name, password="TestPass123!"
    )
    return accounts["brokerage"]


async def _make_global_stock(symbol: str, isin: str) -> tuple[int, int]:
    """A global (security-master) instrument + company, as live-priced US tickers are."""
    async with postgres.async_session_maker() as session:
        company = Company(workspace_id=None, name=f"{symbol} Corp", ticker=symbol, isin=isin)
        session.add(company)
        await session.flush()
        instrument = Instrument(
            workspace_id=None,
            symbol=symbol,
            name=f"{symbol} Corp",
            instrument_type="stock",
            isin=isin,
            exchange="NASDAQ",
            company_id=company.id,
        )
        session.add(instrument)
        await session.commit()
        return company.id, instrument.id


@pytest.mark.asyncio
async def test_set_sector_by_holding_isin_and_ticker_then_read_back(client, tools):
    brokerage = await _register(client, "sector-keys")
    ws = (await _workspace_ids())[0]
    aapl = await _buy(client, brokerage, "AAPL", "1", "100.00")
    msft = await _buy(client, brokerage, "MSFT", "1", "100.00")
    await _buy(client, brokerage, "TSLA", "1", "100.00")
    _, _, msft_company = await _holding_company(msft)
    async with postgres.async_session_maker() as session:
        company = await session.get(Company, msft_company.id)
        company.isin = "US5949181045"
        tsla_instrument = (
            await session.execute(select(Instrument).where(Instrument.symbol == "TSLA"))
        ).scalar_one()
        tsla_instrument.exchange = "NASDAQ"
        await session.commit()

    rows = [
        {"holding_public_id": aapl, "sector": "information technology", "industry": "Hardware"},
        {"isin": "us5949181045", "sector": "Information  Technology", "country_code": "us"},
        {"ticker": "tsla", "exchange": "nasdaq", "sector": "Consumer Discretionary"},
    ]
    preview = await tools["set_company_sector"](workspace_id=ws, rows=rows, confirmed=False)
    assert preview["status"] == "needs_confirmation"
    assert (await _holding_company(aapl))[2].sector is None  # preview wrote nothing

    res = await tools["set_company_sector"](workspace_id=ws, rows=rows, confirmed=True)
    assert res["status"] == "success", res
    assert res["errors"] == []
    assert [u["identified_via"] for u in res["updated"]] == ["holding", "isin", "ticker+exchange"]
    first = res["updated"][0]
    assert first["before"] == {"sector": None, "industry": None, "country_code": None}
    assert first["after"]["sector"] == "Information Technology"  # GICS canonicalised
    assert first["after"]["industry"] == "Hardware"
    assert res["updated"][1]["after"]["country_code"] == "US"

    listing = await tools["list_investment_holdings"](workspace_id=ws, limit=50)
    by_symbol = {h["symbol"]: h for h in listing["items"]}
    assert by_symbol["AAPL"]["company_sector"] == "Information Technology"
    assert by_symbol["AAPL"]["company_industry"] == "Hardware"
    assert by_symbol["MSFT"]["company_sector"] == "Information Technology"
    assert by_symbol["TSLA"]["company_sector"] == "Consumer Discretionary"

    # Idempotent re-run: same values, nothing reported as changed.
    again = await tools["set_company_sector"](workspace_id=ws, rows=rows, confirmed=True)
    assert [u["changed"] for u in again["updated"]] == [False, False, False]


@pytest.mark.asyncio
async def test_bulk_32_holdings_in_one_confirmed_call(client, tools):
    brokerage = await _register(client, "sector-bulk32")
    ws = (await _workspace_ids())[0]
    holding_ids = [await _buy(client, brokerage, f"BULK{i:02d}", "1", "10.00") for i in range(32)]
    rows = [{"holding_public_id": hid, "sector": "Industrials"} for hid in holding_ids]

    res = await tools["set_company_sector"](workspace_id=ws, rows=rows, confirmed=True)
    assert res["status"] == "success", res
    assert len(res["updated"]) == 32 and res["errors"] == []

    listing = await tools["list_investment_holdings"](workspace_id=ws, limit=50)
    assert len(listing["items"]) == 32
    assert {h["company_sector"] for h in listing["items"]} == {"Industrials"}


@pytest.mark.asyncio
async def test_global_company_is_forked_never_mutated_and_rerun_is_idempotent(client, tools):
    brokerage = await _register(client, "sector-fork")
    ws = (await _workspace_ids())[0]
    global_company_id, global_instrument_id = await _make_global_stock("GLBL", "US0000000001")
    holding = await _buy(client, brokerage, "GLBL", "2", "50.00")
    _, instrument, company = await _holding_company(holding)
    assert instrument.id == global_instrument_id and company.workspace_id is None

    rows = [{"holding_public_id": holding, "sector": "Energy", "industry": "Oil"}]
    res = await tools["set_company_sector"](workspace_id=ws, rows=rows, confirmed=True)
    assert res["updated"][0]["forked_from_global"] is True

    async with postgres.async_session_maker() as session:
        global_company = await session.get(Company, global_company_id)
        assert global_company.sector is None and global_company.industry is None
        global_instrument = await session.get(Instrument, global_instrument_id)
        assert global_instrument.company_id == global_company_id
    h2, instrument2, company2 = await _holding_company(holding)
    assert instrument2.workspace_id == ws and company2.workspace_id == ws
    assert company2.sector == "Energy" and company2.isin == "US0000000001"
    assert h2.instrument_id == instrument2.id != global_instrument_id

    # Re-run by ISIN and by holding: resolves to the workspace copy, no second fork.
    res2 = await tools["set_company_sector"](
        workspace_id=ws, rows=[{"isin": "US0000000001", "sector": "Energy"}], confirmed=True
    )
    assert res2["updated"][0]["forked_from_global"] is False
    assert res2["updated"][0]["changed"] is False
    res3 = await tools["set_company_sector"](workspace_id=ws, rows=rows, confirmed=True)
    assert res3["updated"][0]["forked_from_global"] is False
    res4 = await tools["set_company_sector"](
        workspace_id=ws,
        rows=[{"ticker": "GLBL", "exchange": "NASDAQ", "sector": "Energy"}],
        confirmed=True,
    )
    assert res4["updated"][0]["forked_from_global"] is False and res4["errors"] == []
    async with postgres.async_session_maker() as session:
        n_companies = await session.scalar(
            select(func.count()).select_from(Company).where(Company.isin == "US0000000001")
        )
        n_instruments = await session.scalar(
            select(func.count()).select_from(Instrument).where(Instrument.symbol == "GLBL")
        )
    assert (n_companies, n_instruments) == (2, 2)  # one global + one workspace copy each


@pytest.mark.asyncio
async def test_two_holdings_of_one_global_instrument_both_repointed(client, tools):
    brokerage = await _register(client, "sector-fork2")
    ws = (await _workspace_ids())[0]
    await _make_global_stock("GLB2", "US0000000002")
    h1 = await _buy(client, brokerage, "GLB2", "1", "10.00")
    res = await client.post(
        "/v1/finance/accounts",
        json={"name": "second", "account_type": "brokerage", "default_currency_code": "USD"},
    )
    assert res.status_code == 201, res.text
    h2 = await _buy(client, res.json()["public_id"], "GLB2", "1", "10.00")

    out = await tools["set_company_sector"](
        workspace_id=ws,
        rows=[{"ticker": "GLB2", "exchange": "NASDAQ", "sector": "Utilities"}],
        confirmed=True,
    )
    assert out["updated"][0]["forked_from_global"] is True
    for hid in (h1, h2):
        _, instrument, company = await _holding_company(hid)
        assert instrument.workspace_id == ws and company.sector == "Utilities"


@pytest.mark.asyncio
async def test_workspace_isolation_of_sector_writes(client, tools):
    brokerage_a = await _register(client, "sector-iso-a")
    holding_a = await _buy(client, brokerage_a, "ISOA", "1", "10.00")
    brokerage_b = await _register(client, "sector-iso-b")
    holding_b = await _buy(client, brokerage_b, "ISOA", "1", "10.00")
    ws_a, ws_b = await _workspace_ids()

    # B cannot reach A's holding through B's workspace id.
    res = await tools["set_company_sector"](
        workspace_id=ws_b,
        rows=[{"holding_public_id": holding_a, "sector": "Energy"}],
        confirmed=True,
    )
    assert res["updated"] == []
    assert "not found in this workspace" in res["errors"][0]["message"]
    assert (await _holding_company(holding_a))[2].sector is None

    # Same symbol in both workspaces: a write in A leaves B's company alone.
    ok = await tools["set_company_sector"](
        workspace_id=ws_a,
        rows=[{"holding_public_id": holding_a, "sector": "Energy"}],
        confirmed=True,
    )
    assert ok["updated"][0]["after"]["sector"] == "Energy"
    assert (await _holding_company(holding_b))[2].sector is None

    # B's allocation is unaffected by A's sectors.
    alloc = await tools["get_investment_sector_allocation"](workspace_id=ws_b, lookthrough=True)
    assert {s["sector"] for s in alloc["sectors"]} == {"Other / not itemised"}


@pytest.mark.asyncio
async def test_failed_row_rolls_back_its_partial_writes_but_keeps_other_rows(
    client, tools, monkeypatch
):
    brokerage = await _register(client, "sector-partial")
    ws = (await _workspace_ids())[0]
    await _make_global_stock("PART", "US0000000003")
    bad = await _buy(client, brokerage, "PART", "1", "10.00")
    good = await _buy(client, brokerage, "GOOD", "1", "10.00")

    original = CompanyRepository.fork_for_workspace

    async def fork_then_fail(self, workspace_id, company):
        forked = await original(self, workspace_id, company)
        if company.workspace_id is None:
            raise SectorRowError("simulated failure after the fork was written")
        return forked

    monkeypatch.setattr(CompanyRepository, "fork_for_workspace", fork_then_fail)
    res = await tools["set_company_sector"](
        workspace_id=ws,
        rows=[
            {"holding_public_id": bad, "sector": "Energy"},
            {"holding_public_id": good, "sector": "Energy"},
        ],
        confirmed=True,
    )
    assert [e["row_index"] for e in res["errors"]] == [0]
    assert [u["row_index"] for u in res["updated"]] == [1]
    monkeypatch.undo()
    async with postgres.async_session_maker() as session:
        forks = await session.scalar(
            select(func.count())
            .select_from(Company)
            .where(Company.workspace_id == ws, Company.isin == "US0000000003")
        )
    assert forks == 0  # the failed row's fork was rolled back to its savepoint
    _, instrument, company = await _holding_company(bad)
    assert instrument.workspace_id is None and company.workspace_id is None
    assert (await _holding_company(good))[2].sector == "Energy"


@pytest.mark.asyncio
async def test_row_errors_do_not_block_other_rows(client, tools):
    brokerage = await _register(client, "sector-errors")
    ws = (await _workspace_ids())[0]
    good = await _buy(client, brokerage, "OKAY", "1", "10.00")
    res = await tools["set_company_sector"](
        workspace_id=ws,
        rows=[
            {"holding_public_id": str(uuid.uuid4()), "sector": "Energy"},
            {"holding_public_id": good, "sector": "Energy"},
            {"isin": "ZZ0000000000", "sector": "Energy"},
            {"ticker": "NOPE", "exchange": "XNYS", "sector": "Energy"},
        ],
        confirmed=True,
    )
    assert res["status"] == "success"
    assert [u["row_index"] for u in res["updated"]] == [1]
    assert [e["row_index"] for e in res["errors"]] == [0, 2, 3]
    assert (await _holding_company(good))[2].sector == "Energy"


@pytest.mark.asyncio
async def test_unexpected_failure_commits_nothing(client, tools, monkeypatch):
    brokerage = await _register(client, "sector-boom")
    ws = (await _workspace_ids())[0]
    first = await _buy(client, brokerage, "BOOM1", "1", "10.00")
    second = await _buy(client, brokerage, "BOOM2", "1", "10.00")

    original_save = CompanyRepository.save
    calls = {"n": 0}

    async def flaky_save(self, entity):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database went away")
        return await original_save(self, entity)

    monkeypatch.setattr(CompanyRepository, "save", flaky_save)
    res = await tools["set_company_sector"](
        workspace_id=ws,
        rows=[
            {"holding_public_id": first, "sector": "Energy"},
            {"holding_public_id": second, "sector": "Energy"},
        ],
        confirmed=True,
    )
    assert res["status"] == "error" and "no changes were saved" in res["message"]
    monkeypatch.undo()
    # The first row's write was rolled back with the rest.
    assert (await _holding_company(first))[2].sector is None
    assert (await _holding_company(second))[2].sector is None


@pytest.mark.asyncio
async def test_constituent_sector_never_mutates_a_global_company(client, tools):
    brokerage = await _register(client, "sector-constituent-global")
    ws = (await _workspace_ids())[0]
    global_company_id, _ = await _make_global_stock("KO", "US1912161007")
    fund_id = await _create_instrument(client, "FUNDG", instrument_type="mutual_fund")
    holding = await _buy(
        client,
        brokerage,
        "FUNDG",
        "1",
        "1000.00",
        instrument_type="mutual_fund",
        instrument_name="Global fund",
    )
    await _submit_holding_price(client, holding, "1000.00")

    row = {
        "company_name": "KO Corp",
        "company_ticker": "KO",
        "company_isin": "US1912161007",
        "weight": "1.0",
        "sector": "consumer staples",
    }
    await _write_snapshot(client, fund_id, [row])
    await _write_snapshot(
        client, fund_id, [row], as_of_date="2026-11-02", fetched_at="2026-11-02T10:00:00Z"
    )

    async with postgres.async_session_maker() as session:
        global_company = await session.get(Company, global_company_id)
        assert global_company.sector is None  # untouched
        copies = (
            (
                await session.execute(
                    select(Company).where(
                        Company.workspace_id == ws, Company.isin == "US1912161007"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(copies) == 1 and copies[0].sector == "Consumer Staples"

    alloc = await tools["get_investment_sector_allocation"](workspace_id=ws, lookthrough=True)
    assert [s["sector"] for s in alloc["sectors"]] == ["Consumer Staples"]


@pytest.mark.asyncio
async def test_case_variants_share_one_allocation_bucket(client, tools):
    brokerage = await _register(client, "sector-casing")
    ws = (await _workspace_ids())[0]
    one = await _buy(client, brokerage, "CASEA", "1", "100.00")
    two = await _buy(client, brokerage, "CASEB", "1", "300.00")
    await _submit_holding_price(client, one, "100.00")
    await _submit_holding_price(client, two, "300.00")
    # Legacy rows written before normalisation existed (direct DB edit).
    async with postgres.async_session_maker() as session:
        for symbol, sector in (("CASEA", "Tech  services"), ("CASEB", "tech services")):
            instrument = (
                await session.execute(select(Instrument).where(Instrument.symbol == symbol))
            ).scalar_one()
            company = await session.get(Company, instrument.company_id)
            company.sector = sector
        await session.commit()

    alloc = await tools["get_investment_sector_allocation"](workspace_id=ws, lookthrough=True)
    assert len(alloc["sectors"]) == 1
    assert alloc["sectors"][0]["value"] == "400.00" and alloc["sectors"][0]["pct"] == 100.0


@pytest.mark.asyncio
async def test_percentages_and_values_sum_exactly(client, tools):
    brokerage = await _register(client, "sector-rounding")
    ws = (await _workspace_ids())[0]
    rows = []
    for symbol, sector in (("RNDA", "Energy"), ("RNDB", "Utilities"), ("RNDC", "Materials")):
        hid = await _buy(client, brokerage, symbol, "1", "33.33")
        await _submit_holding_price(client, hid, "33.33")
        rows.append({"holding_public_id": hid, "sector": sector})
    await tools["set_company_sector"](workspace_id=ws, rows=rows, confirmed=True)

    alloc = await tools["get_investment_sector_allocation"](
        workspace_id=ws, as_of_date=date.today().isoformat(), lookthrough=True
    )
    assert alloc["holdings_value"] == "99.99"
    assert sum(Decimal(s["value"]) for s in alloc["sectors"]) == Decimal("99.99")
    assert sum(Decimal(str(s["pct"])) for s in alloc["sectors"]) == Decimal("100.00")


@pytest.mark.asyncio
async def test_constituents_read_uses_single_newest_snapshot(client):
    await _register(client, "sector-read-newest")
    fund_id = await _create_instrument(client, "FUNDR", instrument_type="mutual_fund")
    await _write_snapshot(
        client,
        fund_id,
        [_constituent("Johnson & Johnson", "JNJ", "1.0", "Health Care")],
        source="old",
        fetched_at="2026-11-01T10:00:00Z",
    )
    await _write_snapshot(
        client,
        fund_id,
        [_constituent("Procter & Gamble", "PG", "1.0", "Consumer Staples")],
        source="new",
        fetched_at="2026-11-01T11:00:00Z",
    )
    res = await client.get(
        f"/v1/investing/instruments/{fund_id}/constituents", params={"as_of": "2026-11-01"}
    )
    assert res.status_code == 200, res.text
    assert [r["company_name"] for r in res.json()] == ["Procter & Gamble"]
    async with postgres.async_session_maker() as session:
        total = await session.scalar(select(func.count()).select_from(InstrumentConstituent))
    assert total == 2  # both snapshots stored; only the newest is read


@pytest.mark.asyncio
async def test_fork_repoints_workspace_constituent_rows_to_copy(client, tools):
    """Bug 4: forking a global company via set_company_sector re-points this
    workspace's constituent rows (on workspace-owned instruments) to the copy,
    so look-through doesn't keep pointing at the now-stale global record.
    """
    brokerage = await _register(client, "sector-fork-repoint")
    ws = (await _workspace_ids())[0]
    async with postgres.async_session_maker() as session:
        fan = Company(workspace_id=None, name="FANUY Corp", ticker="FANUY", isin=None)
        session.add(fan)
        await session.flush()
        fan_id = fan.id
        await session.commit()

    fund_id = await _create_instrument(client, "FUNDF", instrument_type="mutual_fund")
    holding = await _buy(
        client,
        brokerage,
        "FUNDF",
        "1",
        "1000.00",
        instrument_type="mutual_fund",
        instrument_name="Fan fund",
    )
    await _submit_holding_price(client, holding, "1000.00")

    async with postgres.async_session_maker() as session:
        instrument = await session.get(
            Instrument,
            (
                await session.execute(
                    select(Holding.instrument_id).where(Holding.public_id == uuid.UUID(holding))
                )
            ).scalar_one(),
        )
        instrument.company_id = fan_id
        await session.commit()

    await _write_snapshot(
        client,
        fund_id,
        [_constituent("FANUY Corp", "FANUY", "1.0", "Industrials")],
    )

    # Bug 4: the snapshot write forks the global company into this workspace and
    # re-points this workspace's constituent rows (on workspace-owned
    # instruments) to the copy, so look-through doesn't keep pointing at the
    # now-stale global record.
    async with postgres.async_session_maker() as session:
        fund = await session.execute(select(Instrument.id).where(Instrument.public_id == fund_id))
        rows = (
            (
                await session.execute(
                    select(InstrumentConstituent.constituent_company_id).where(
                        InstrumentConstituent.instrument_id == fund.scalar_one()
                    )
                )
            )
            .scalars()
            .all()
        )
        copies = (
            (await session.execute(select(Company.workspace_id).where(Company.id.in_(rows))))
            .scalars()
            .all()
        )
    assert rows, "constituent rows were written"
    assert all(wid == ws for wid in copies), copies  # all point at a workspace copy

    # Re-running set_company_sector resolves to the workspace copy (idempotent,
    # not a re-fork) and is a no-op.
    res = await tools["set_company_sector"](
        workspace_id=ws,
        rows=[{"holding_public_id": holding, "sector": "Energy"}],
        confirmed=True,
    )
    assert res["status"] == "success", res
    assert res["updated"][0]["forked_from_global"] is False
    # The snapshot write already set Industrials on the copy, so this re-sets
    # it to Energy: a real change, not a re-fork.
    assert res["updated"][0]["changed"] is True
    assert res["updated"][0]["after"]["sector"] == "Energy"


@pytest.mark.asyncio
async def test_set_company_details_rename_and_isin_confirmed(client, tools, monkeypatch):
    await _register(client, "company-details")
    ws = (await _workspace_ids())[0]
    async with postgres.async_session_maker() as session:
        user_id = await _registered_user_id(session, "company-details@example.com")
    # authorize_workspace is mocked to return 1 by the `tools` fixture, but the
    # registered user's id is sequence-assigned and not reliably 1. The real user
    # id is required because set_company_details writes to audit_logs (FK -> users).
    monkeypatch.setattr("app.mcp.server.authorize_workspace", AsyncMock(return_value=user_id))

    async with postgres.async_session_maker() as session:
        company = Company(workspace_id=ws, name="Old Name", ticker="OLD", isin=None)
        session.add(company)
        await session.flush()
        public_id = str(company.public_id)
        await session.commit()

    preview = await tools["set_company_details"](
        workspace_id=ws, company_public_id=public_id, name="New Name", confirmed=False
    )
    assert preview["status"] == "needs_confirmation"
    async with postgres.async_session_maker() as session:
        row = await session.get(Company, company.id)
        assert row.name == "Old Name"  # preview wrote nothing

    res = await tools["set_company_details"](
        workspace_id=ws,
        company_public_id=public_id,
        name="New Name",
        isin="USNEW0000001",
        confirmed=True,
    )
    assert res["status"] == "success", res
    assert res["changed"] is True
    assert res["before"] == {"name": "Old Name", "isin": None}
    assert res["after"] == {"name": "New Name", "isin": "USNEW0000001"}
    async with postgres.async_session_maker() as session:
        row = await session.get(Company, company.id)
        assert row.name == "New Name"
        assert row.isin == "USNEW0000001"


@pytest.mark.asyncio
async def test_set_company_details_forks_global_never_mutates(client, tools, monkeypatch):
    await _register(client, "company-details-fork")
    ws = (await _workspace_ids())[0]
    async with postgres.async_session_maker() as session:
        user_id = await _registered_user_id(session, "company-details-fork@example.com")
    monkeypatch.setattr("app.mcp.server.authorize_workspace", AsyncMock(return_value=user_id))

    async with postgres.async_session_maker() as session:
        company = Company(workspace_id=None, name="Global Co", ticker="GLBL", isin="USGLBL0000001")
        session.add(company)
        await session.flush()
        public_id = str(company.public_id)
        global_id = company.id
        await session.commit()

    res = await tools["set_company_details"](
        workspace_id=ws, company_public_id=public_id, name="Workspace Co", confirmed=True
    )
    assert res["status"] == "success", res
    assert res["forked_from_global"] is True
    assert res["changed"] is True
    assert res["after"]["name"] == "Workspace Co"
    assert res["after"]["isin"] == "USGLBL0000001"

    async with postgres.async_session_maker() as session:
        global_row = await session.get(Company, global_id)
        assert global_row.workspace_id is None
        assert global_row.name == "Global Co"  # global untouched
        copies = (
            (
                await session.execute(
                    select(Company).where(
                        Company.workspace_id == ws, Company.name == "Workspace Co"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(copies) == 1


@pytest.mark.asyncio
async def test_set_company_details_rejects_duplicate_isin_and_name(client, tools):
    await _register(client, "company-details-conflict")
    ws = (await _workspace_ids())[0]
    async with postgres.async_session_maker() as session:
        a = Company(workspace_id=ws, name="Company A", ticker="A", isin="USA0000000001")
        b = Company(workspace_id=ws, name="Company B", ticker="B", isin="USB0000000002")
        session.add_all([a, b])
        await session.flush()
        aid = str(a.public_id)
        await session.commit()

    # ISIN already used by Company B.
    res = await tools["set_company_details"](
        workspace_id=ws, company_public_id=aid, isin="USB0000000002", confirmed=True
    )
    assert res["status"] == "error", res
    assert "already used" in res["message"]

    # Name already used by Company B.
    res = await tools["set_company_details"](
        workspace_id=ws, company_public_id=aid, name="Company B", confirmed=True
    )
    assert res["status"] == "error", res
    assert "already named" in res["message"]

    # Unknown company.
    res = await tools["set_company_details"](
        workspace_id=ws, company_public_id=str(uuid.uuid4()), name="Nope", confirmed=True
    )
    assert res["status"] == "error", res
    assert "not found in this workspace" in res["message"]


@pytest.mark.asyncio
async def test_set_company_details_idempotent_noop(client, tools, monkeypatch):
    await _register(client, "company-details-noop")
    ws = (await _workspace_ids())[0]
    async with postgres.async_session_maker() as session:
        user_id = await _registered_user_id(session, "company-details-noop@example.com")
    monkeypatch.setattr("app.mcp.server.authorize_workspace", AsyncMock(return_value=user_id))

    async with postgres.async_session_maker() as session:
        company = Company(workspace_id=ws, name="Same Name", ticker="SAME", isin="USSAME0000001")
        session.add(company)
        await session.flush()
        public_id = str(company.public_id)
        await session.commit()

    res = await tools["set_company_details"](
        workspace_id=ws, company_public_id=public_id, name="Same Name", confirmed=True
    )
    assert res["status"] == "success", res
    assert res["changed"] is False
    assert res["before"] == res["after"] == {"name": "Same Name", "isin": "USSAME0000001"}
