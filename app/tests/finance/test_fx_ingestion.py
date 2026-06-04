from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import pytest
from sqlmodel import select

from app.application.jobs import fx_rate_ingestion_job
from app.application.workflows import ingest_fx_rates
from app.config import settings
from app.core.database import postgres
from app.finance.models import FxRate


@pytest.fixture(autouse=True)
async def clean_fx_rates(override_database_url):
    """Ensure a clean slate for FX rates before each test."""
    async with postgres.async_session_maker() as session, session.begin():
        # Delete existing rates to avoid unique constraint violations across test runs
        await session.execute(select(FxRate).execution_options(synchronize_session="fetch"))
        # Just flush and commit
        # Let's delete them:
        res = await session.execute(select(FxRate))
        for rate in res.scalars().all():
            await session.delete(rate)


@pytest.mark.asyncio
async def test_ingest_fx_rates_success(client, monkeypatch):
    # Mock settings key
    monkeypatch.setattr(settings, "EXCHANGERATE_API_KEY", "test-api-key")

    # Mock response data
    mock_data = {
        "result": "success",
        "time_last_update_unix": 1717430400,  # 2024-06-03 16:00:00 UTC
        "conversion_rates": {
            "USD": 1.0,
            "GBP": 0.8,
            "INR": 83.0,
        },
    }

    # Mock HTTP GET response
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_data
    mock_resp.raise_for_status = MagicMock()

    async def mock_get(*args, **kwargs):
        return mock_resp

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    # Run ingestion workflow
    async with postgres.async_session_maker() as session, session.begin():
        await ingest_fx_rates(session)

    # Verify rates are written in the database
    async with postgres.async_session_maker() as session:
        rates_res = await session.execute(select(FxRate))
        rates = rates_res.scalars().all()

        assert len(rates) == 6

        rate_map = {
            (r.base_currency_code, r.quote_currency_code): Decimal(str(r.rate)) for r in rates
        }

        assert rate_map[("USD", "GBP")] == Decimal("0.8").quantize(Decimal("1.0000000000"))
        assert rate_map[("GBP", "USD")] == (Decimal("1.0") / Decimal("0.8")).quantize(
            Decimal("1.0000000000")
        )
        assert rate_map[("USD", "INR")] == Decimal("83.0").quantize(Decimal("1.0000000000"))
        assert rate_map[("INR", "USD")] == (Decimal("1.0") / Decimal("83.0")).quantize(
            Decimal("1.0000000000")
        )
        assert rate_map[("GBP", "INR")] == (Decimal("83.0") / Decimal("0.8")).quantize(
            Decimal("1.0000000000")
        )
        assert rate_map[("INR", "GBP")] == (Decimal("0.8") / Decimal("83.0")).quantize(
            Decimal("1.0000000000")
        )

        # Verify source and as_of
        for r in rates:
            assert r.source == "exchangerate-api"
            assert r.as_of == datetime.fromtimestamp(1717430400, UTC)


@pytest.mark.asyncio
async def test_ingest_fx_rates_missing_key(client, monkeypatch):
    monkeypatch.setattr(settings, "EXCHANGERATE_API_KEY", None)

    async with postgres.async_session_maker() as session:
        with pytest.raises(ValueError, match="EXCHANGERATE_API_KEY.*not configured"):
            await ingest_fx_rates(session)


@pytest.mark.asyncio
async def test_ingest_fx_rates_api_error_response(client, monkeypatch):
    monkeypatch.setattr(settings, "EXCHANGERATE_API_KEY", "test-api-key")

    mock_data = {"result": "error", "error-type": "invalid-key"}

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_data
    mock_resp.raise_for_status = MagicMock()

    async def mock_get(*args, **kwargs):
        return mock_resp

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    async with postgres.async_session_maker() as session:
        with pytest.raises(ValueError, match="ExchangeRate-API request failed: invalid-key"):
            await ingest_fx_rates(session)


@pytest.mark.asyncio
async def test_ingest_fx_rates_http_error(client, monkeypatch):
    monkeypatch.setattr(settings, "EXCHANGERATE_API_KEY", "test-api-key")

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        message="Internal Server Error", request=MagicMock(), response=mock_resp
    )

    async def mock_get(*args, **kwargs):
        return mock_resp

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    async with postgres.async_session_maker() as session:
        with pytest.raises(httpx.HTTPStatusError):
            await ingest_fx_rates(session)


@pytest.mark.asyncio
async def test_ingest_fx_rates_missing_conversion_rates(client, monkeypatch):
    monkeypatch.setattr(settings, "EXCHANGERATE_API_KEY", "test-api-key")

    # Missing INR rate
    mock_data = {
        "result": "success",
        "conversion_rates": {
            "USD": 1.0,
            "GBP": 0.8,
        },
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_data
    mock_resp.raise_for_status = MagicMock()

    async def mock_get(*args, **kwargs):
        return mock_resp

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    async with postgres.async_session_maker() as session:
        with pytest.raises(KeyError, match="Expected currency code 'INR' missing"):
            await ingest_fx_rates(session)


@pytest.mark.asyncio
async def test_fx_rate_ingestion_job_orchestration(client, monkeypatch):
    called = False

    async def mock_ingest(session):
        nonlocal called
        called = True

    monkeypatch.setattr("app.application.jobs.ingest_fx_rates", mock_ingest)

    # Run the job
    await fx_rate_ingestion_job()

    # Verify workflow was called
    assert called is True


@pytest.mark.asyncio
async def test_fx_rate_ingestion_job_propagates_exception(client, monkeypatch):
    async def mock_ingest_fail(session):
        raise ValueError("Boom")

    monkeypatch.setattr("app.application.jobs.ingest_fx_rates", mock_ingest_fail)

    # Run job and verify exception is propagated
    with pytest.raises(ValueError, match="Boom"):
        await fx_rate_ingestion_job()
