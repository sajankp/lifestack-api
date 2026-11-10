"""Company sectors and look-through sector allocation (spec-100).

DB-backed tests exercising constituent sector propagation, the
no-overwrite-by-default rule, the single-newest-snapshot look-through rule,
and the sector allocation endpoint summing to 100% including
``Other / not itemised``.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.database import postgres
from app.finance.models import FxRate
from app.finance.repository import FinanceSettingRepository, FxRateRepository
from app.investing.repository import (
    CompanyRepository,
    HoldingPriceRepository,
    HoldingRepository,
    InstrumentConstituentRepository,
    InstrumentRepository,
)
from app.investing.service import ExposureAnalyticsService
from app.investing.theme_repository import (
    HoldingThemeTagRepository,
    LatestConstituentSnapshotRepository,
)
from app.investing.theme_service import ThemeExposureService
from app.platform.models import Workspace
from app.tests.integration.test_investing import (
    _create_brokerage_account,
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
    currency: str = "USD",
) -> str:
    return await _create_holding_via_order(
        client,
        account_id,
        symbol,
        quantity,
        price,
        instrument_type=instrument_type,
        instrument_name=instrument_name,
        currency=currency,
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
    await _submit_holding_price(client, voo_holding, "2000.00", date(2026, 11, 8).isoformat())
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


@pytest.mark.asyncio
async def test_lookthrough_engines_agree_on_shared_fixture(client: AsyncClient) -> None:
    """RED-GREEN probe: the two look-through engines must value the same
    portfolio identically.

    Fixture: AAPL (direct, USD) and VTI (ETF, USD, snapshotted into AAPL/MSFT)
    are priced; FUND2 (mutual fund, INR, snapshotted) has NO market price.

    Before the fix, ExposureAnalyticsService.sector_allocation valued the
    unpriced fund at average cost while ThemeExposureService.lookthrough_exposure
    excluded it with a warning -- two different totals for the same portfolio.
    After the fix sector_allocation delegates valuation to lookthrough_exposure,
    so both agree and the unpriced fund is excluded with an explicit warning,
    never silently re-based to cost.
    """
    account_map = await _register_and_login(
        client,
        email="lookthrough-engines-agree@example.com",
        username="lookthroughenginesagree",
        password="TestPass123!",
    )
    brokerage = account_map["brokerage"]
    inr_account = await _create_brokerage_account(client, "INR Brokerage", "INR")

    settings_res = await client.patch(
        "/v1/finance/settings",
        json={"reporting_currency_code": "INR"},
    )
    assert settings_res.status_code == 200, settings_res.text

    await _create_instrument(client, "AAPL")
    await _create_instrument(client, "MSFT")
    vti_id = await _create_instrument(
        client, "VTI", instrument_type="etf", name="Vanguard Total Market ETF"
    )
    await _create_instrument(client, "FUND2", instrument_type="mutual_fund", name="Test Fund 2")

    aapl_holding = await _buy(client, brokerage, "AAPL", "2.00000000", "200.00")
    vti_holding = await _buy(
        client,
        brokerage,
        "VTI",
        "10.00000000",
        "120.00",
        instrument_type="etf",
        instrument_name="Vanguard Total Market ETF",
    )
    await _buy(
        client,
        inr_account,
        "FUND2",
        "5.00000000",
        "200.00",
        instrument_type="mutual_fund",
        instrument_name="Test Fund 2",
        currency="INR",
    )

    today = datetime.now(UTC).date()
    await _submit_holding_price(client, aapl_holding, "200.00", today.isoformat())
    await _submit_holding_price(client, vti_holding, "120.00", today.isoformat())
    # FUND2 deliberately has no market price.

    await _write_snapshot(
        client,
        vti_id,
        [
            _constituent("Apple Inc", "AAPL", "0.60000000", "Information Technology"),
            _constituent("Microsoft Corp", "MSFT", "0.40000000", "Information Technology"),
        ],
    )

    async with postgres.async_session_maker() as session:
        session.add(
            FxRate(
                base_currency_code="USD",
                quote_currency_code="INR",
                rate=Decimal("80"),
                as_of=datetime.now(UTC) - timedelta(days=1),
                fetched_at=datetime.now(UTC),
                source="test",
            )
        )
        await session.commit()
        workspaces = (await session.execute(select(Workspace))).scalars().all()
        assert workspaces
        ws_id = workspaces[0].id

        analytics = ExposureAnalyticsService(
            HoldingRepository(session),
            InstrumentRepository(session),
            CompanyRepository(session),
            InstrumentConstituentRepository(session),
            HoldingPriceRepository(session),
            FinanceSettingRepository(session),
            FxRateRepository(session),
        )
        exposure_service = ThemeExposureService(
            HoldingRepository(session),
            InstrumentRepository(session),
            CompanyRepository(session),
            LatestConstituentSnapshotRepository(session),
            HoldingPriceRepository(session),
            FinanceSettingRepository(session),
            FxRateRepository(session),
            HoldingThemeTagRepository(session),
        )

        allocated = await analytics.sector_allocation(ws_id, today, lookthrough=True)
        exposed = await exposure_service.lookthrough_exposure(ws_id, today)

    # Side by side: the engines must now return identical valuations.
    allocated_total = Decimal(allocated["holdings_value"])
    exposed_total = Decimal(exposed.total_value)
    assert allocated_total == exposed_total == Decimal("128000.00"), (
        f"engines disagree: sector_allocation={allocated_total} "
        f"lookthrough_exposure={exposed_total}"
    )

    by_sector = {row["sector"]: Decimal(row["value"]) for row in allocated["sectors"]}
    assert by_sector == {"Information Technology": Decimal("128000.00")}, allocated["sectors"]
    pct_sum = round(sum(row["pct"] for row in allocated["sectors"]), 2)
    assert pct_sum == 100.0, allocated["sectors"]

    # The unpriced fund is excluded with an explicit warning in both engines,
    # never silently valued at cost.
    assert any("no market price" in w for w in allocated["warnings"])
    assert any("no market price" in w for w in exposed.warnings)
    assert {row["symbol"] for row in allocated["fund_coverage"]} == {"VTI"}

    # The REST allocation endpoint agrees with the look-through engine on
    # holdings_value; total_portfolio_value additionally includes cash.
    alloc_res = await client.get(
        "/v1/investing/analytics/allocation", params={"lookthrough": "true"}
    )
    assert alloc_res.status_code == 200, alloc_res.text
    alloc = alloc_res.json()
    assert Decimal(alloc["holdings_value"]) == Decimal("128000.00")
    assert Decimal(alloc["total_portfolio_value"]) == Decimal(alloc["holdings_value"]) + Decimal(
        alloc["cash_value"]
    )


@pytest.mark.asyncio
async def test_allocation_endpoint_excludes_unconvertible_holding(client: AsyncClient) -> None:
    """Regression probe for the audit-reported mixed-valuation REST bug.

    /v1/investing/analytics/allocation converted the reporting currency differently
    from the look-through engine and, when an FX rate was missing, added the
    holding's RAW native value to the reporting-currency totals instead of
    excluding it -- mixing two valuations in one response.

    Fixture: AAPL (USD, priced, USD->INR rate present) and VOD (GBP, priced, no
    GBP->INR rate). Reporting currency INR. VOD must be excluded from
    holdings_value with a warning, never added at its raw GBP value.

    Before the fix: holdings_value == 81000.00 (80000 INR + 1000 raw GBP).
    After the fix: holdings_value == 80000.00 and the response matches
    /v1/investing/analytics/lookthrough-exposure.
    """
    account_map = await _register_and_login(
        client,
        email="allocation-no-fx@example.com",
        username="allocationnofx",
        password="TestPass123!",
    )
    usd_brokerage = account_map["brokerage"]
    gbp_account = await _create_brokerage_account(client, "GBP Brokerage", "GBP")

    settings_res = await client.patch(
        "/v1/finance/settings",
        json={"reporting_currency_code": "INR"},
    )
    assert settings_res.status_code == 200, settings_res.text

    await _create_instrument(client, "AAPL")
    await _create_instrument(client, "VOD")

    today = datetime.now(UTC).date()
    aapl_holding = await _buy(client, usd_brokerage, "AAPL", "10.00000000", "100.00")  # $1,000
    await _submit_holding_price(client, aapl_holding, "100.00", today.isoformat())
    vod_holding = await _buy(
        client, gbp_account, "VOD", "10.00000000", "100.00", currency="GBP"
    )  # £1,000
    await _submit_holding_price(client, vod_holding, "100.00", today.isoformat())

    async with postgres.async_session_maker() as session:
        session.add(
            FxRate(
                base_currency_code="USD",
                quote_currency_code="INR",
                rate=Decimal("80"),
                as_of=datetime.now(UTC) - timedelta(days=1),
                fetched_at=datetime.now(UTC),
                source="test",
            )
        )
        await session.commit()

    async with postgres.async_session_maker() as session:
        workspaces = (await session.execute(select(Workspace))).scalars().all()
        ws_id = workspaces[0].id
        analytics = ExposureAnalyticsService(
            HoldingRepository(session),
            InstrumentRepository(session),
            CompanyRepository(session),
            InstrumentConstituentRepository(session),
            HoldingPriceRepository(session),
            FinanceSettingRepository(session),
            FxRateRepository(session),
        )
        allocated = await analytics.sector_allocation(ws_id, today, lookthrough=False)

    assert Decimal(allocated["holdings_value"]) == Decimal("80000.00"), allocated
    assert any("VOD" in w and "GBP" in w for w in allocated["warnings"]), allocated["warnings"]

    res = await client.get("/v1/investing/analytics/allocation", params={"lookthrough": "false"})
    assert res.status_code == 200, res.text
    after = res.json()
    assert Decimal(after["holdings_value"]) == Decimal("80000.00"), after
    assert Decimal(after["total_portfolio_value"]) == Decimal(after["holdings_value"]) + Decimal(
        after["cash_value"]
    )
    assert any("VOD" in w and "GBP" in w and "INR" in w for w in after["warnings"]), after[
        "warnings"
    ]

    exposure_res = await client.get(
        "/v1/investing/analytics/lookthrough-exposure", params={"as_of": today}
    )
    assert exposure_res.status_code == 200, exposure_res.text
    assert Decimal(exposure_res.json()["total_value"]) == Decimal("80000.00")
