"""Unit tests for ExposureAnalyticsService.sector_allocation (spec-100), no DB."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.investing.sectors import normalise_sector
from app.investing.service import ExposureAnalyticsService

AS_OF = date(2026, 11, 8)
OTHER = "Other / not itemised"


def _holding(id_, symbol, qty, currency="USD", instrument_id=None, avg_cost="1"):
    return SimpleNamespace(
        id=id_,
        symbol=symbol,
        quantity=Decimal(qty),
        currency=currency,
        instrument_id=instrument_id if instrument_id is not None else id_,
        avg_cost=Decimal(avg_cost),
    )


def _instrument(id_, symbol, kind="stock", company_id=None):
    return SimpleNamespace(
        id=id_,
        public_id=uuid.uuid4(),
        symbol=symbol,
        name=symbol,
        instrument_type=kind,
        company_id=company_id,
    )


def _company(id_, sector, public_id=None, name=None, ticker=None, country_code=None):
    return SimpleNamespace(
        id=id_,
        public_id=public_id or uuid.uuid4(),
        name=name or f"Company {id_}",
        ticker=ticker,
        sector=sector,
        country_code=country_code,
    )


def _snapshot_row(company_id, weight, as_of=date(2026, 11, 1), source="sec"):
    return SimpleNamespace(
        constituent_company_id=company_id,
        weight=Decimal(weight),
        as_of_date=as_of,
        source=source,
    )


def _service(
    holdings,
    instruments,
    companies,
    snapshots=None,
    prices=None,
    reporting_currency=None,
    fx=None,
):
    queries = {"constituents": 0, "companies": 0, "instruments": 0}

    class Holdings:
        async def get_all(self, workspace_id, limit=None, offset=0, quantity_state=None):
            return holdings, len(holdings)

    class Instruments:
        async def get_by_ids(self, ids):
            queries["instruments"] += 1
            return {i.id: i for i in instruments if i.id in set(ids)}

        async def get_by_symbols(self, workspace_id, symbols):
            return {}

    class Companies:
        async def get_by_ids(self, ids):
            queries["companies"] += 1
            return {c.id: c for c in companies if c.id in set(ids)}

    class Constituents:
        async def get_latest_on_or_before_many(self, ids, as_of):
            queries["constituents"] += 1
            return {i: rows for i, rows in (snapshots or {}).items() if i in set(ids)}

        # Same query as LatestConstituentSnapshotRepository.get_single_latest_many:
        # exactly one snapshot per instrument, newest by (as_of_date, fetched_at).
        get_single_latest_many = get_latest_on_or_before_many

    class Prices:
        async def latest_prices_on_or_before_bulk(self, workspace_id, holding_ids, as_of):
            return {
                hid: SimpleNamespace(unit_price=Decimal(price))
                for hid, price in (prices or {}).items()
            }

    class Settings:
        async def get_by_workspace(self, workspace_id):
            return SimpleNamespace(reporting_currency_code=reporting_currency)

    class Fx:
        async def get_latest_rates_for_pairs(self, pairs, as_of):
            return {
                key: SimpleNamespace(rate=Decimal(rate), as_of=datetime(2026, 11, 8, tzinfo=UTC))
                for key, rate in (fx or {}).items()
            }

    service = ExposureAnalyticsService(
        Holdings(), Instruments(), Companies(), Constituents(), Prices(), Settings(), Fx()
    )
    return service, queries


@pytest.mark.asyncio
async def test_fx_conversion_and_missing_fx_warns_instead_of_zero():
    holdings = [
        _holding(1, "USSTK", "1", "USD"),
        _holding(2, "INSTK", "10", "INR"),
        _holding(3, "GBSTK", "1", "GBP"),  # no GBP rate available
    ]
    instruments = [
        _instrument(1, "USSTK", company_id=1),
        _instrument(2, "INSTK", company_id=2),
        _instrument(3, "GBSTK", company_id=3),
    ]
    companies = [_company(1, "Energy"), _company(2, "Financials"), _company(3, "Utilities")]
    service, _ = _service(
        holdings,
        instruments,
        companies,
        prices={1: "100", 2: "100", 3: "100"},
        reporting_currency="USD",
        fx={("INR", "USD"): "0.01", ("USD", "INR"): "100"},
    )
    result = await service.sector_allocation(1, AS_OF)

    by_sector = {s["sector"]: s for s in result["sectors"]}
    assert by_sector["Energy"]["value"] == "100.00"
    assert by_sector["Financials"]["value"] == "10.00"  # 1000 INR * 0.01
    assert "Utilities" not in by_sector  # excluded, not silently zero
    assert result["holdings_value"] == "110.00"
    assert any("no FX rate" in w and "GBSTK" in w for w in result["warnings"])
    assert sum(Decimal(str(s["pct"])) for s in result["sectors"]) == Decimal("100.00")


@pytest.mark.asyncio
async def test_missing_price_excludes_instead_of_valuing_at_cost() -> None:
    """A holding with no market price is excluded with an explicit warning;
    it is never silently re-based to average cost. That keeps sector_allocation
    and lookthrough_exposure on the same market-value basis."""
    holdings = [_holding(1, "NOPRICE", "2", avg_cost="25")]
    service, _ = _service(
        holdings, [_instrument(1, "NOPRICE", company_id=1)], [_company(1, "Energy")]
    )
    result = await service.sector_allocation(1, AS_OF)
    assert result["sectors"] == []
    assert result["holdings_value"] == "0.00"
    assert result["currency"] == "USD"
    assert any("no market price" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_multi_currency_without_reporting_currency_defaults_to_inr() -> None:
    """With no reporting currency set on a multi-currency portfolio the
    valuation defaults to INR (the look-through engine's default) and converts
    every holding, instead of defaulting to USD."""
    holdings = [_holding(1, "A", "1", "USD"), _holding(2, "B", "1", "EUR")]
    service, _ = _service(
        holdings,
        [_instrument(1, "A", company_id=1), _instrument(2, "B", company_id=1)],
        [_company(1, "Energy")],
        prices={1: "10", 2: "10"},
        fx={("EUR", "INR"): "2", ("USD", "INR"): "80"},
    )
    result = await service.sector_allocation(1, AS_OF)
    assert result["currency"] == "INR"
    assert result["holdings_value"] == "820.00"  # 10 USD x 80 + 10 EUR x 2
    assert any("No reporting currency configured; valuing in INR" in w for w in result["warnings"])
    by_sector = {s["sector"]: Decimal(s["value"]) for s in result["sectors"]}
    assert by_sector == {"Energy": Decimal("820.00")}


@pytest.mark.asyncio
async def test_multi_currency_with_explicit_usd_setting() -> None:
    """An explicit workspace reporting currency still wins over the default."""
    holdings = [_holding(1, "A", "1", "USD"), _holding(2, "B", "1", "EUR")]
    service, _ = _service(
        holdings,
        [_instrument(1, "A", company_id=1), _instrument(2, "B", company_id=1)],
        [_company(1, "Energy")],
        prices={1: "10", 2: "10"},
        reporting_currency="USD",
        fx={("EUR", "USD"): "2"},
    )
    result = await service.sector_allocation(1, AS_OF)
    assert result["currency"] == "USD"
    assert result["holdings_value"] == "30.00"  # 10 USD + 10 EUR x 2


@pytest.mark.asyncio
async def test_overweight_snapshot_never_exceeds_fund_value_and_unmapped_goes_to_other():
    fund = _instrument(10, "FUND", kind="etf")
    holdings = [_holding(10, "FUND", "1")]
    snapshots = {10: [_snapshot_row(1, "0.6"), _snapshot_row(2, "0.41"), _snapshot_row(3, "0.0")]}
    companies = [_company(1, "Energy"), _company(2, None), _company(3, "Utilities")]
    service, _ = _service(holdings, [fund], companies, snapshots=snapshots, prices={10: "1000"})
    result = await service.sector_allocation(1, AS_OF)

    assert sum(Decimal(s["value"]) for s in result["sectors"]) == Decimal("1000.00")
    by_sector = {s["sector"]: Decimal(s["value"]) for s in result["sectors"]}
    assert set(by_sector) == {"Energy", OTHER}  # unmapped company -> Other
    assert (
        result["fund_coverage"][0]["coverage_pct"] == 100.0
    )  # every row resolves to a known company


@pytest.mark.asyncio
async def test_partial_coverage_remainder_is_other_not_rescaled():
    fund = _instrument(10, "FUND", kind="mutual_fund")
    holdings = [_holding(10, "FUND", "1")]
    service, _ = _service(
        holdings,
        [fund],
        [_company(1, "Health Care")],
        snapshots={10: [_snapshot_row(1, "0.25")]},
        prices={10: "400"},
    )
    result = await service.sector_allocation(1, AS_OF)
    assert result["sectors"] == [
        {"sector": OTHER, "value": "300.00", "pct": 75.0},
        {"sector": "Health Care", "value": "100.00", "pct": 25.0},
    ]
    assert result["fund_coverage"][0]["coverage_pct"] == 25.0


@pytest.mark.asyncio
async def test_stale_snapshot_warns_and_placeholder_not_in_coverage():
    fund = _instrument(10, "FUND", kind="etf")
    placeholder = _instrument(11, "OTHER-FUND", company_id=None)
    holdings = [_holding(10, "FUND", "1"), _holding(11, "other-fund", "1")]
    service, _ = _service(
        holdings,
        [fund, placeholder],
        [_company(1, "Energy")],
        snapshots={10: [_snapshot_row(1, "1.0", as_of=date(2025, 1, 1))]},
        prices={10: "100", 11: "50"},
    )
    result = await service.sector_allocation(1, AS_OF)
    assert any("is stale" in w for w in result["warnings"])
    assert [f["symbol"] for f in result["fund_coverage"]] == ["FUND"]
    assert {s["sector"]: s["value"] for s in result["sectors"]} == {
        "Energy": "100.00",
        OTHER: "50.00",
    }


@pytest.mark.asyncio
async def test_without_lookthrough_funds_are_one_bucket():
    fund = _instrument(10, "FUND", kind="etf")
    stock = _instrument(11, "STK", company_id=None)
    holdings = [_holding(10, "FUND", "1"), _holding(11, "STK", "1")]
    service, queries = _service(holdings, [fund, stock], [], prices={10: "70", 11: "30"})
    result = await service.sector_allocation(1, AS_OF, lookthrough=False)
    assert {s["sector"]: s["pct"] for s in result["sectors"]} == {
        "ETFs & Funds": 70.0,
        "Unclassified": 30.0,
    }
    assert queries["constituents"] == 0


@pytest.mark.asyncio
async def test_allocation_batches_queries_regardless_of_holding_count():
    n = 40
    instruments = [_instrument(i, f"S{i}", company_id=i) for i in range(1, n + 1)]
    holdings = [_holding(i, f"S{i}", "1") for i in range(1, n + 1)]
    fund = _instrument(100, "FUND", kind="etf")
    holdings.append(_holding(100, "FUND", "1"))
    snapshot = {100: [_snapshot_row(i, "0.025") for i in range(1, n + 1)]}
    service, queries = _service(
        holdings,
        [*instruments, fund],
        [_company(i, "Energy") for i in range(1, n + 1)],
        snapshots=snapshot,
        prices={h.id: "10" for h in holdings},
    )
    await service.sector_allocation(1, AS_OF)
    assert queries == {"constituents": 1, "companies": 1, "instruments": 1}


@pytest.mark.asyncio
async def test_empty_portfolio_returns_no_sectors():
    service, _ = _service([], [], [])
    result = await service.sector_allocation(1, AS_OF)
    assert result["sectors"] == [] and result["holdings_value"] == "0.00"


def test_sector_normalisation_rules():
    assert normalise_sector("  health   CARE ") == "Health Care"
    assert normalise_sector("other / NOT itemised") == OTHER
    assert normalise_sector("Semiconductor  Equipment") == "Semiconductor Equipment"
    assert normalise_sector("   ") is None
    assert normalise_sector(None) is None
