from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import pytest
from sqlmodel import select

from app.application.constituent_provider import (
    ConstituentEntry,
    ConstituentProviderResult,
    YahooFinanceConstituentProvider,
)
from app.application.jobs import constituent_ingestion_job
from app.application.workflows import ingest_constituents
from app.core.database import postgres
from app.investing.models import Instrument, InstrumentConstituent
from app.platform.models import Workspace


class FakeConstituentProvider:
    def __init__(self):
        self.calls: list[str] = []

    async def fetch(self, symbol: str) -> ConstituentProviderResult | None:
        self.calls.append(symbol)
        if symbol == "FAIL":
            return None
        return ConstituentProviderResult(
            symbol=symbol,
            provider_key="test-provider-top-n-normalised",
            fetched_at=datetime.now(UTC),
            constituents=[
                ConstituentEntry("Apple Inc", "AAPL", Decimal("0.05")),
                ConstituentEntry("Microsoft Corp", "MSFT", Decimal("0.04")),
                ConstituentEntry("Nvidia Corp", "NVDA", Decimal("0.03")),
            ],
        )


@pytest.mark.asyncio
async def test_yahoo_constituent_provider_parses_top_holdings(monkeypatch):
    mock_data = {
        "quoteSummary": {
            "result": [
                {
                    "topHoldings": {
                        "holdings": [
                            {
                                "holdingName": "Apple Inc",
                                "symbol": "AAPL",
                                "holdingPercent": {"raw": 0.0734},
                            },
                            {
                                "holdingName": "Microsoft Corp",
                                "symbol": "MSFT",
                                "holdingPercent": {"raw": 0.062},
                            },
                        ]
                    }
                }
            ]
        }
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = mock_data
    mock_resp.raise_for_status = MagicMock()

    async def mock_get(*args, **kwargs):
        return mock_resp

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    result = await YahooFinanceConstituentProvider().fetch("spy")

    assert result is not None
    assert result.symbol == "SPY"
    assert result.provider_key == "yahoo-finance-top-n-normalised"
    assert result.constituents[0].company_name == "Apple Inc"
    assert result.constituents[0].company_ticker == "AAPL"
    assert result.constituents[0].raw_weight == Decimal("0.0734")


@pytest.mark.asyncio
async def test_ingest_constituents_end_to_end_normalises_weights(client):
    provider = FakeConstituentProvider()

    async with postgres.async_session_maker() as session, session.begin():
        workspace = Workspace(name="Constituent Test")
        session.add(workspace)
        await session.flush()
        workspace_id = workspace.id
        session.add(
            Instrument(
                workspace_id=workspace.id,
                symbol="VUSA",
                name="Vanguard S&P 500 ETF",
                instrument_type="etf",
            )
        )
        session.add(
            Instrument(
                workspace_id=workspace.id,
                symbol="AAPL",
                name="Apple Inc",
                instrument_type="stock",
            )
        )

    async with postgres.async_session_maker() as session, session.begin():
        result = await ingest_constituents(session, provider=provider, staleness_days=0)

    assert result == {f"{workspace_id}:VUSA": "ok"}
    assert provider.calls == ["VUSA"]

    async with postgres.async_session_maker() as session:
        rows = (await session.execute(select(InstrumentConstituent))).scalars().all()
        assert len(rows) == 3
        assert sum((row.weight for row in rows), Decimal("0")).quantize(
            Decimal("1.00000000")
        ) == Decimal("1.00000000")
        assert {row.source for row in rows} == {"test-provider-top-n-normalised"}


@pytest.mark.asyncio
async def test_ingest_constituents_staleness_guard_skips_fresh_snapshot(client):
    provider = FakeConstituentProvider()

    async with postgres.async_session_maker() as session, session.begin():
        workspace = Workspace(name="Constituent Staleness Test")
        session.add(workspace)
        await session.flush()
        workspace_id = workspace.id
        session.add(
            Instrument(
                workspace_id=workspace.id,
                symbol="VTI",
                name="Vanguard Total Stock Market ETF",
                instrument_type="etf",
            )
        )

    async with postgres.async_session_maker() as session, session.begin():
        first = await ingest_constituents(session, provider=provider, staleness_days=7)
    async with postgres.async_session_maker() as session, session.begin():
        second = await ingest_constituents(session, provider=provider, staleness_days=7)

    assert first == {f"{workspace_id}:VTI": "ok"}
    assert second == {f"{workspace_id}:VTI": "skipped"}
    assert provider.calls == ["VTI"]


@pytest.mark.asyncio
async def test_constituent_ingestion_job_orchestration(client, monkeypatch):
    called = False

    async def mock_ingest(session, staleness_days=None):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr("app.application.jobs.ingest_constituents", mock_ingest)

    await constituent_ingestion_job()

    assert called is True


@pytest.mark.asyncio
async def test_constituent_ingestion_job_propagates_exception(client, monkeypatch):
    async def mock_ingest(session, staleness_days=None):
        raise ValueError("Boom")

    monkeypatch.setattr("app.application.jobs.ingest_constituents", mock_ingest)

    with pytest.raises(ValueError, match="Boom"):
        await constituent_ingestion_job()
