"""Company sectors and look-through sector allocation (spec-100).

DB-backed tests exercising constituent sector propagation, the
no-overwrite-by-default rule, the single-newest-snapshot look-through rule,
and the sector allocation endpoint summing to 100% including
``Other / not itemised``.
"""

from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.database import postgres
from app.investing.repository import (
    CompanyRepository,
    HoldingPriceRepository,
    HoldingRepository,
    InstrumentConstituentRepository,
    InstrumentRepository,
)
from app.investing.service import ExposureAnalyticsService
from app.platform.models import Workspace
from app.tests.integration.test_investing import (
    _create_holding_via_order,
    _register_and_login,
    _submit_holding_price,
)


@pytest.fixture(autouse=True)
def _no_network_price_lookup(monkeypatch):
    """Keep instruments/companies workspace-scoped (no live price lookup)."""

    async def _none(*args, **kwargs):
        return None

    monkeypatch.setattr("app.investing.service._fetch_stock_price", _none)


_SNAPSHOT_DATE = date(2026, 11, 1)
_FETCHED_T1 = "2026-11-01T10:00:00Z"
_FETCHED_T2 = "2026-11-01T11:00:00Z"


async def _create_instrument(
    client: AsyncClient,
    symbol: str,
    *,
    instrument_type: str = "stock",
    name: str | None = None,
) -> str:
    res = await client.post(
        "/v1/investing/instruments",
        json={
            "symbol": symbol,
            "name": name or symbol,
            "instrument_type": instrument_type,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["public_id"]


async def _instrument_public_id(client: AsyncClient, symbol: str) -> str:
    res = await client.get("/v1/investing/instruments")
    assert res.status_code == 200, res.text
    instruments = res.json()
    matches = [i for i in instruments if i["symbol"] == symbol.upper()]
    assert matches, f"no instrument {symbol}"
    return matches[0]["public_id"]


async def _buy(
    client: AsyncClient,
    account_id: str,
    symbol: str,
    quantity: str,
    price: str,
    *,
    instrument_type: str = "stock",
    instrument_name: str | None = None,
) -> str:
    return await _create_holding_via_order(
        client,
        account_id,
        symbol,
        quantity,
        price,
        instrument_type=instrument_type,
        instrument_name=instrument_name,
    )


async def _write_snapshot(
    client: AsyncClient,
    instrument_public_id: str,
    rows: list[dict],
    *,
    as_of_date: str = _SNAPSHOT_DATE.isoformat(),
    source: str = "sec",
    fetched_at: str = _FETCHED_T1,
    overwrite_existing: bool = False,
) -> None:
    payload = {
        "as_of_date": as_of_date,
        "source": source,
        "fetched_at": fetched_at,
        "constituents": rows,
        "renormalise": False,
        "overwrite_existing": overwrite_existing,
    }
    res = await client.post(
        f"/v1/investing/instruments/{instrument_public_id}/constituents", json=payload
    )
    assert res.status_code == 201, res.text


def _constituent(
    company_name: str,
    company_ticker: str,
    weight: str,
    sector: str,
) -> dict:
    return {
        "company_name": company_name,
        "company_ticker": company_ticker,
        "weight": weight,
        "sector": sector,
    }


@pytest.mark.asyncio
async def test_constituent_sector_fills_once_and_overwrites_only_with_flag(
    client: AsyncClient,
) -> None:
    account_map = await _register_and_login(
        client,
        email="sector-overwrite@example.com",
        username="sectoroverwrite",
        password="TestPass123!",
    )
    brokerage = account_map["brokerage"]

    aapl_holding = await _buy(client, brokerage, "AAPL", "10.00000000", "100.00")
    await _submit_holding_price(client, aapl_holding, "100.00")

    voo_id = await _create_instrument(
        client, "VOO", instrument_type="mutual_fund", name="Vanguard 500 Index Fund"
    )
    voo_holding = await _buy(
        client,
        brokerage,
        "VOO",
        "1.00000000",
        "2000.00",
        instrument_type="mutual_fund",
        instrument_name="Vanguard 500 Index Fund",
    )
    await _submit_holding_price(client, voo_holding, "2000.00")

    # First snapshot sets Apple's sector to Information Technology.
    await _write_snapshot(
        client, voo_id, [_constituent("Apple Inc", "AAPL", "1.0", "Information Technology")]
    )
    res = await client.get(
        f"/v1/investing/instruments/{voo_id}/constituents",
        params={"as_of": _SNAPSHOT_DATE.isoformat()},
    )
    assert res.status_code == 200, res.text
    assert res.json()[0]["sector"] == "Information Technology"

    # Second snapshot with a different sector but no overwrite flag: unchanged.
    await _write_snapshot(
        client,
        voo_id,
        [_constituent("Apple Inc", "AAPL", "1.0", "Health Care")],
        as_of_date="2026-11-02",
        fetched_at="2026-11-02T10:00:00Z",
    )
    res = await client.get(
        f"/v1/investing/instruments/{voo_id}/constituents",
        params={"as_of": "2026-11-02"},
    )
    assert res.status_code == 200, res.text
    assert res.json()[0]["sector"] == "Information Technology"

    # With overwrite_existing the sector changes.
    await _write_snapshot(
        client,
        voo_id,
        [_constituent("Apple Inc", "AAPL", "1.0", "Health Care")],
        as_of_date="2026-11-03",
        fetched_at="2026-11-03T10:00:00Z",
        overwrite_existing=True,
    )
    res = await client.get(
        f"/v1/investing/instruments/{voo_id}/constituents",
        params={"as_of": "2026-11-03"},
    )
    assert res.status_code == 200, res.text
    assert res.json()[0]["sector"] == "Health Care"


@pytest.mark.asyncio
async def test_lookthrough_sector_allocation_sums_to_100_with_placeholder(
    client: AsyncClient,
) -> None:
    account_map = await _register_and_login(
        client,
        email="sector-lookthrough@example.com",
        username="sectorlookthrough",
        password="TestPass123!",
    )
    brokerage = account_map["brokerage"]

    # Direct stock first: its company is created with no sector.
    aapl_holding = await _buy(client, brokerage, "AAPL", "10.00000000", "100.00")
    await _submit_holding_price(client, aapl_holding, "100.00")

    # Fund snapshot next: its Apple row sets that same company's sector to IT.
    voo_id = await _create_instrument(
        client, "VOO", instrument_type="mutual_fund", name="Vanguard 500 Index Fund"
    )
    voo_holding = await _buy(
        client,
        brokerage,
        "VOO",
        "1.00000000",
        "2000.00",
        instrument_type="mutual_fund",
        instrument_name="Vanguard 500 Index Fund",
    )
    await _submit_holding_price(client, voo_holding, "2000.00")
    await _write_snapshot(
        client,
        voo_id,
        [
            _constituent("Apple Inc", "AAPL", "0.5", "Information Technology"),
            _constituent("Johnson & Johnson", "JNJ", "0.5", "Health Care"),
        ],
    )

    # Placeholder holding: full value to "Other / not itemised", never rescaled.
    other_holding = await _buy(client, brokerage, "OTHER-VOO", "1.00000000", "600.00")
    await _submit_holding_price(client, other_holding, "600.00")

    res = await client.get("/v1/investing/analytics/allocation", params={"lookthrough": "true"})
    assert res.status_code == 200, res.text
    data = res.json()

    holdings_value = Decimal(data["holdings_value"])
    assert holdings_value == Decimal("3600.00"), data

    by_sector = {row["sector"]: (Decimal(row["value"]), row["pct"]) for row in data["sectors"]}
    assert by_sector["Information Technology"][0] == Decimal("2000.00")
    assert by_sector["Health Care"][0] == Decimal("1000.00")
    assert by_sector["Other / not itemised"][0] == Decimal("600.00")
    pct_sum = round(sum(pct for _, pct in by_sector.values()), 2)
    assert pct_sum == 100.0, by_sector


@pytest.mark.asyncio
async def test_lookthrough_uses_only_the_newest_snapshot_per_instrument(
    client: AsyncClient,
) -> None:
    account_map = await _register_and_login(
        client,
        email="sector-newest@example.com",
        username="sectornewest",
        password="TestPass123!",
    )
    brokerage = account_map["brokerage"]

    fund_holding = await _buy(
        client,
        brokerage,
        "FUND",
        "1.00000000",
        "2000.00",
        instrument_type="mutual_fund",
        instrument_name="Test Fund",
    )
    await _submit_holding_price(client, fund_holding, "2000.00")
    fund_id = await _instrument_public_id(client, "FUND")

    # Same date, two sources: the later fetched_at wins.
    await _write_snapshot(
        client,
        fund_id,
        [_constituent("Johnson & Johnson", "JNJ", "1.0", "Health Care")],
        source="old",
        fetched_at=_FETCHED_T1,
    )
    await _write_snapshot(
        client,
        fund_id,
        [_constituent("Procter & Gamble", "PG", "1.0", "Consumer Staples")],
        source="new",
        fetched_at=_FETCHED_T2,
    )

    res = await client.get("/v1/investing/analytics/allocation", params={"lookthrough": "true"})
    assert res.status_code == 200, res.text
    data = res.json()

    holdings_value = Decimal(data["holdings_value"])
    assert holdings_value == Decimal("2000.00"), data

    by_sector = {row["sector"]: (Decimal(row["value"]), row["pct"]) for row in data["sectors"]}
    assert by_sector["Consumer Staples"][0] == Decimal("2000.00")
    assert "Health Care" not in by_sector


@pytest.mark.asyncio
async def test_lookthrough_without_snapshot_lands_in_other_not_itemised(
    client: AsyncClient,
) -> None:
    account_map = await _register_and_login(
        client,
        email="sector-no-snapshot@example.com",
        username="sectornosnapshot",
        password="TestPass123!",
    )
    brokerage = account_map["brokerage"]

    fund_holding = await _buy(
        client,
        brokerage,
        "FUND",
        "1.00000000",
        "2000.00",
        instrument_type="mutual_fund",
        instrument_name="Test Fund",
    )
    await _submit_holding_price(client, fund_holding, "2000.00")

    res = await client.get("/v1/investing/analytics/allocation", params={"lookthrough": "true"})
    assert res.status_code == 200, res.text
    data = res.json()

    holdings_value = Decimal(data["holdings_value"])
    assert holdings_value == Decimal("2000.00"), data

    by_sector = {row["sector"]: (Decimal(row["value"]), row["pct"]) for row in data["sectors"]}
    assert set(by_sector) == {"Other / not itemised"}
    assert by_sector["Other / not itemised"][0] == Decimal("2000.00")
    assert round(sum(pct for _, pct in by_sector.values()), 2) == 100.0


@pytest.mark.asyncio
async def test_sector_allocation_fund_coverage(client: AsyncClient) -> None:
    """Coverage % per fund comes from the snapshot weights (real DB path)."""
    account_map = await _register_and_login(
        client,
        email="sector-coverage@example.com",
        username="sectorcoverage",
        password="TestPass123!",
    )
    brokerage = account_map["brokerage"]

    voo_id = await _create_instrument(
        client, "VOO", instrument_type="mutual_fund", name="Vanguard 500 Index Fund"
    )
    voo_holding = await _buy(
        client,
        brokerage,
        "VOO",
        "1.00000000",
        "2000.00",
        instrument_type="mutual_fund",
        instrument_name="Vanguard 500 Index Fund",
    )
    await _submit_holding_price(client, voo_holding, "2000.00")
    await _write_snapshot(
        client,
        voo_id,
        [
            _constituent("Apple Inc", "AAPL", "0.6", "Information Technology"),
            _constituent("Johnson & Johnson", "JNJ", "0.4", "Health Care"),
        ],
    )

    async with postgres.async_session_maker() as session:
        workspaces = (await session.execute(select(Workspace))).scalars().all()
        assert workspaces
        service = ExposureAnalyticsService(
            HoldingRepository(session),
            InstrumentRepository(session),
            CompanyRepository(session),
            InstrumentConstituentRepository(session),
            HoldingPriceRepository(session),
            None,
            None,
        )
        result = await service.sector_allocation(
            workspaces[0].id, date(2026, 11, 8), lookthrough=True
        )
        coverage = {row["symbol"]: row for row in result["fund_coverage"]}
        assert coverage["VOO"]["coverage_pct"] == 100.0
        assert coverage["VOO"]["constituent_rows"] == 2
        assert coverage["VOO"]["snapshot_date"] == "2026-11-01"
        assert result["sectors"][0]["sector"] == "Information Technology"
