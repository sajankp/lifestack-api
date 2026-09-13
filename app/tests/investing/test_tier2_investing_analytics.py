import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.investing.models import (
    Company,
    Dividend,
    Holding,
    Instrument,
    InstrumentConstituent,
    InstrumentType,
)
from app.investing.performance_service import PerformanceService
from app.investing.schemas import (
    DividendHistoryResponse,
    PortfolioAllocationResponse,
)
from app.investing.service import ExposureAnalyticsService


@pytest.fixture(scope="session", autouse=True)
def override_redis_url():
    yield None


@pytest.mark.asyncio
async def test_allocation_breakdown_calculation():
    holding_repo = MagicMock()
    holding_repo.session = AsyncMock()
    cash_repo = MagicMock()
    holding_price_repo = MagicMock()
    snapshot_repo = MagicMock()
    instrument_repo = MagicMock()
    account_repo = MagicMock()

    # Two holdings: 1 stock (Apple), 1 ETF (VOO)
    h1 = Holding(
        id=1,
        workspace_id=1,
        account_id=10,
        instrument_id=101,
        symbol="AAPL",
        quantity=Decimal("10"),
        cost_basis=Decimal("1500.00"),
        avg_cost=Decimal("150.00"),
        currency="USD",
    )
    h2 = Holding(
        id=2,
        workspace_id=1,
        account_id=10,
        instrument_id=102,
        symbol="VOO",
        quantity=Decimal("5"),
        cost_basis=Decimal("2000.00"),
        avg_cost=Decimal("400.00"),
        currency="USD",
    )
    holding_repo.get_all = AsyncMock(return_value=([h1, h2], 2))

    # Cash balance
    cash_balance = MagicMock()
    cash_balance.account_id = 10
    cash_balance.balance = Decimal("500.00")
    cash_balance.currency = "USD"
    cash_repo.get_latest_per_account_currency = AsyncMock(return_value=[cash_balance])

    # Brokerage account
    brokerage_acct = MagicMock()
    brokerage_acct.id = 10
    brokerage_acct.account_type = "brokerage"
    account_repo.list_workspace_accounts = AsyncMock(return_value=([brokerage_acct], 1))

    # Holding prices: AAPL = 200 (10 * 200 = 2000), VOO = 450 (5 * 450 = 2250)
    p1 = MagicMock()
    p1.unit_price = Decimal("200.00")
    p2 = MagicMock()
    p2.unit_price = Decimal("450.00")
    holding_price_repo.latest_prices_on_or_before_bulk = AsyncMock(return_value={1: p1, 2: p2})

    # Instruments
    inst1 = Instrument(
        id=101,
        workspace_id=1,
        symbol="AAPL",
        name="Apple Inc",
        instrument_type=InstrumentType.stock,
        company_id=201,
    )
    inst2 = Instrument(
        id=102,
        workspace_id=1,
        symbol="VOO",
        name="Vanguard S&P 500 ETF",
        instrument_type=InstrumentType.etf,
        company_id=None,
    )
    instrument_repo.get_by_ids = AsyncMock(return_value={101: inst1, 102: inst2})

    # Company for AAPL
    comp1 = Company(
        id=201,
        workspace_id=1,
        name="Apple Inc",
        sector="Technology",
    )
    mock_comp_res = MagicMock()
    mock_comp_res.scalars.return_value.all.return_value = [comp1]
    holding_repo.session.execute = AsyncMock(return_value=mock_comp_res)

    service = PerformanceService(
        holding_repo=holding_repo,
        cash_repo=cash_repo,
        holding_price_repo=holding_price_repo,
        snapshot_repo=snapshot_repo,
        instrument_repo=instrument_repo,
        account_repo=account_repo,
    )

    res = await service.get_allocation_breakdown(workspace_id=1, as_of=date(2026, 9, 13))

    assert isinstance(res, PortfolioAllocationResponse)
    # Total portfolio = 2000 (AAPL) + 2250 (VOO) + 500 (Cash) = 4750.00
    assert res.total_portfolio_value == Decimal("4750.00")
    assert res.holdings_value == Decimal("4250.00")
    assert res.cash_value == Decimal("500.00")

    class_map = {item.key: item for item in res.asset_classes}
    assert class_map["stock"].value == Decimal("2000.00")
    assert class_map["etf"].value == Decimal("2250.00")
    assert class_map["cash"].value == Decimal("500.00")

    # Sector verification
    sector_map = {s.sector: s for s in res.sectors}
    assert sector_map["Technology"].value == Decimal("2000.00")
    assert sector_map["ETFs & Funds"].value == Decimal("2250.00")


@pytest.mark.asyncio
async def test_dividend_history_aggregation():
    holding_repo = MagicMock()
    holding_repo.session = AsyncMock()

    d1 = Dividend(
        id=1,
        public_id=uuid.uuid4(),
        workspace_id=1,
        account_id=10,
        symbol="AAPL",
        gross_amount=Decimal("100.00"),
        tax_withheld=Decimal("15.00"),
        net_amount=Decimal("85.00"),
        currency="USD",
        pay_date=date(2026, 7, 15),
    )
    d2 = Dividend(
        id=2,
        public_id=uuid.uuid4(),
        workspace_id=1,
        account_id=10,
        symbol="MSFT",
        gross_amount=Decimal("200.00"),
        tax_withheld=Decimal("30.00"),
        net_amount=Decimal("170.00"),
        currency="USD",
        pay_date=date(2026, 7, 20),
    )
    d3 = Dividend(
        id=3,
        public_id=uuid.uuid4(),
        workspace_id=1,
        account_id=10,
        symbol="AAPL",
        gross_amount=Decimal("110.00"),
        tax_withheld=Decimal("16.50"),
        net_amount=Decimal("93.50"),
        currency="USD",
        pay_date=date(2026, 8, 15),
    )

    mock_div_res = MagicMock()
    mock_div_res.scalars.return_value.all.return_value = [d1, d2, d3]
    holding_repo.session.execute = AsyncMock(return_value=mock_div_res)

    service = PerformanceService(
        holding_repo=holding_repo,
        cash_repo=MagicMock(),
        holding_price_repo=MagicMock(),
        snapshot_repo=MagicMock(),
    )

    res = await service.get_dividend_history(workspace_id=1)

    assert isinstance(res, DividendHistoryResponse)
    # Total received = 85 + 170 + 93.50 = 348.50
    assert res.total_dividends_received == Decimal("348.50")
    assert len(res.monthly_history) == 2

    m1 = res.monthly_history[0]
    assert m1.month == "2026-07"
    assert m1.gross_amount == Decimal("300.00")
    assert m1.net_amount == Decimal("255.00")
    assert m1.payment_count == 2

    m2 = res.monthly_history[1]
    assert m2.month == "2026-08"
    assert m2.net_amount == Decimal("93.50")
    assert m2.payment_count == 1


@pytest.mark.asyncio
async def test_benchmark_alpha_calculation():
    snapshot_repo = MagicMock()
    s1 = MagicMock()
    s1.snapshot_date = date(2026, 1, 1)
    s1.holdings_value = Decimal("10000.00")
    s1.total_cost = Decimal("8000.00")
    s1.total_value = Decimal("10000.00")
    s1.cash_value = Decimal("0.00")
    s1.currency_code = "USD"

    s2 = MagicMock()
    s2.snapshot_date = date(2026, 7, 1)  # ~181 days later
    s2.holdings_value = Decimal("12000.00")
    s2.total_cost = Decimal("8000.00")
    s2.total_value = Decimal("12000.00")
    s2.cash_value = Decimal("0.00")
    s2.currency_code = "USD"

    snapshot_repo.list_range = AsyncMock(return_value=[s1, s2])

    service = PerformanceService(
        holding_repo=MagicMock(),
        cash_repo=MagicMock(),
        holding_price_repo=MagicMock(),
        snapshot_repo=snapshot_repo,
    )

    res = await service.get_performance_history(workspace_id=1)
    assert res.benchmark_symbol == "SPY"
    assert len(res.points) == 2
    assert res.points[0].benchmark_return_pct == Decimal("0.00")
    assert res.points[0].benchmark_value == Decimal("10000.00")

    # s2 portfolio return = +20% (10000 -> 12000)
    # Benchmark return ~4.8% (10% annualized over half a year)
    assert res.points[1].benchmark_return_pct is not None
    assert res.benchmark_return_pct is not None
    assert res.alpha_pct is not None
    assert res.alpha_pct > Decimal("0")  # Portfolio outperformed benchmark!


@pytest.mark.asyncio
async def test_constituent_quarterly_staleness_threshold():
    """Verify that constituent snapshots within 90 days (1 quarter) are not flagged as stale."""
    holding_repo = MagicMock()
    h = Holding(
        id=1,
        workspace_id=1,
        account_id=10,
        instrument_id=101,
        symbol="VOO",
        quantity=Decimal("10"),
        cost_basis=Decimal("4000.00"),
        avg_cost=Decimal("400.00"),
        currency="USD",
    )
    holding_repo.get_all = AsyncMock(return_value=([h], 1))

    inst = Instrument(
        id=101,
        workspace_id=1,
        symbol="VOO",
        name="Vanguard 500",
        instrument_type=InstrumentType.etf,
        company_id=None,
    )
    instrument_repo = MagicMock()
    instrument_repo.get_by_ids = AsyncMock(return_value={101: inst})
    instrument_repo.get_by_symbols = AsyncMock(return_value={})

    comp = Company(id=201, workspace_id=1, name="Apple", sector="Technology")
    company_repo = MagicMock()
    company_repo.get_by_ids = AsyncMock(return_value={201: comp})

    # Snapshot dated 60 days ago (older than 30d, but within 90d quarter)
    as_of = date(2026, 9, 13)
    snapshot_60d = InstrumentConstituent(
        id=1,
        instrument_id=101,
        constituent_company_id=201,
        weight=Decimal("1.0"),
        as_of_date=date(2026, 7, 15),  # ~60 days before 2026-09-13
        source="factsheet",
    )
    constituent_repo = MagicMock()
    constituent_repo.get_latest_on_or_before_many = AsyncMock(return_value={101: [snapshot_60d]})

    service = ExposureAnalyticsService(
        holding_repo=holding_repo,
        instrument_repo=instrument_repo,
        company_repo=company_repo,
        constituent_repo=constituent_repo,
    )

    res = await service.exposure(workspace_id=1, as_of=as_of)
    # Since staleness threshold is 90 days (1 quarter), 60d snapshot should NOT trigger stale warning
    assert res.staleness_days == 90
    assert not any("Stale constituent snapshot" in w for w in res.warnings)
