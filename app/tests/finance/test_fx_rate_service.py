from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import ValidationError
from app.finance.models import Currency, FxRate
from app.finance.schemas import FxRateUpsert
from app.finance.service import FxRateService


@pytest.fixture
def mock_repo():
    return AsyncMock()


@pytest.fixture
def mock_currency_repo():
    return AsyncMock()


@pytest.fixture
def service(mock_repo, mock_currency_repo):
    return FxRateService(repository=mock_repo, currency_repository=mock_currency_repo)


def _make_fx_rate(base: str, quote: str, rate: float, as_of: datetime = None) -> FxRate:
    return FxRate(
        id=1,
        base_currency_code=base,
        quote_currency_code=quote,
        rate=rate,
        as_of=as_of or datetime.now(UTC),
        fetched_at=datetime.now(UTC),
        source="test",
    )


@pytest.mark.asyncio
async def test_resolve_rate_same_currency(service):
    # Same currency should immediately return 1
    rate = await service.resolve_rate("USD", "USD")
    assert rate == Decimal("1")
    # Repo must not be called
    service.repository.get_latest_rate.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_rate_direct_exists(service, mock_repo):
    as_of = datetime.now(UTC)
    mock_rate = _make_fx_rate("USD", "GBP", 0.8, as_of)
    mock_repo.get_latest_rate.return_value = mock_rate

    rate = await service.resolve_rate("USD", "GBP", as_of=as_of)

    assert rate == Decimal("0.8")
    mock_repo.get_latest_rate.assert_called_once_with("USD", "GBP", as_of=as_of)


@pytest.mark.asyncio
async def test_resolve_rate_triangulation_success(service, mock_repo):
    as_of = datetime.now(UTC)
    # Direct rate doesn't exist, triangulation via USD exists
    mock_repo.get_latest_rate.side_effect = lambda base, quote, as_of=None: {
        ("GBP", "INR"): None,
        ("GBP", "USD"): _make_fx_rate("GBP", "USD", 1.25, as_of),
        ("USD", "INR"): _make_fx_rate("USD", "INR", 80.0, as_of),
    }.get((base, quote))

    rate = await service.resolve_rate("GBP", "INR", as_of=as_of)
    assert rate == Decimal("1.25") * Decimal("80.0")

    mock_repo.get_latest_rate.assert_any_call("GBP", "INR", as_of=as_of)
    mock_repo.get_latest_rate.assert_any_call("GBP", "USD", as_of=as_of)
    mock_repo.get_latest_rate.assert_any_call("USD", "INR", as_of=as_of)


@pytest.mark.asyncio
async def test_resolve_rate_missing_intermediate_base_to_usd(service, mock_repo):
    as_of = datetime.now(UTC)
    mock_repo.get_latest_rate.side_effect = lambda base, quote, as_of=None: {
        ("GBP", "INR"): None,
        ("GBP", "USD"): None,
        ("USD", "INR"): _make_fx_rate("USD", "INR", 80.0, as_of),
    }.get((base, quote))

    rate = await service.resolve_rate("GBP", "INR", as_of=as_of)
    assert rate is None


@pytest.mark.asyncio
async def test_resolve_rate_missing_intermediate_usd_to_quote(service, mock_repo):
    as_of = datetime.now(UTC)
    mock_repo.get_latest_rate.side_effect = lambda base, quote, as_of=None: {
        ("GBP", "INR"): None,
        ("GBP", "USD"): _make_fx_rate("GBP", "USD", 1.25, as_of),
        ("USD", "INR"): None,
    }.get((base, quote))

    rate = await service.resolve_rate("GBP", "INR", as_of=as_of)
    assert rate is None


@pytest.mark.asyncio
async def test_resolve_rate_usd_itself_missing_direct(service, mock_repo):
    mock_repo.get_latest_rate.return_value = None
    rate = await service.resolve_rate("USD", "INR")
    assert rate is None
    mock_repo.get_latest_rate.assert_called_once_with("USD", "INR", as_of=None)


@pytest.mark.asyncio
async def test_upsert_validation_success(service, mock_currency_repo, mock_repo):
    mock_currency_repo.get_by_code.side_effect = lambda code: Currency(code=code, is_active=True)
    payload = FxRateUpsert(
        base_currency_code="GBP",
        quote_currency_code="USD",
        rate=Decimal("1.25"),
        as_of=datetime.now(UTC),
        fetched_at=datetime.now(UTC),
        source="test-provider",
    )

    await service.upsert(payload)
    mock_repo.upsert_rate.assert_called_once()


@pytest.mark.asyncio
async def test_upsert_validation_failure_inactive_currency(service, mock_currency_repo):
    mock_currency_repo.get_by_code.side_effect = lambda code: Currency(
        code=code, is_active=(code == "GBP")
    )
    payload = FxRateUpsert(
        base_currency_code="GBP",
        quote_currency_code="USD",
        rate=Decimal("1.25"),
        as_of=datetime.now(UTC),
        fetched_at=datetime.now(UTC),
        source="test-provider",
    )

    with pytest.raises(ValidationError) as exc:
        await service.upsert(payload)
    assert "Unsupported currency code" in exc.value.detail
