"""Unit tests for the LRS summary service and the stored-FX-rate reads (no DB)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import ValidationError
from app.finance.lrs_service import IST, LrsService, resolve_fy, tcs_threshold_inr
from app.investing.tax_service import (
    MAX_FX_AUDIT_DAYS,
    TaxService,
    normalize_currency_code,
)


def _transfer(gross, rate, to_ccy="USD", occurred_at=None, public_id="t1"):
    return SimpleNamespace(
        public_id=public_id,
        gross_amount=Decimal(gross),
        fx_rate_used=Decimal(rate) if rate is not None else None,
        to_currency_code=to_ccy,
        occurred_at=occurred_at or datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
    )


def _service(transfers, fx_repo=None):
    repo = MagicMock()
    repo.list_outward_remittances = AsyncMock(return_value=transfers)
    return LrsService(repo, fx_repo), repo


def test_tcs_threshold_by_fy():
    assert tcs_threshold_inr(2024) == Decimal("700000.00")
    assert tcs_threshold_inr(2025) == Decimal("1000000.00")
    assert tcs_threshold_inr(2026) == Decimal("1000000.00")


def test_resolve_fy_valid_default_and_invalid():
    assert resolve_fy("2026-27") == ("2026-27", date(2026, 4, 1), date(2027, 3, 31))
    assert resolve_fy("  2025-26 ")[0] == "2025-26"
    assert resolve_fy(None)[0] == resolve_fy("")[0]  # default = current FY
    for bad in ("2026-28", "26-27", "abc", "1999-00", "2200-01", "9999-00"):
        with pytest.raises(ValidationError):
            resolve_fy(bad)


@pytest.mark.asyncio
async def test_summary_queries_workspace_with_ist_fy_bounds():
    service, repo = _service([])
    await service.get_lrs_summary(7, "2026-27")
    repo.list_outward_remittances.assert_awaited_once()
    (workspace_id,), kwargs = repo.list_outward_remittances.call_args
    assert workspace_id == 7
    assert kwargs["start"] == datetime(2026, 3, 31, 18, 30, tzinfo=UTC)  # 1 Apr 00:00 IST
    assert kwargs["end_exclusive"] == datetime(2027, 3, 31, 18, 30, tzinfo=UTC)
    assert kwargs["start"].utcoffset() == IST.utcoffset(None)


@pytest.mark.asyncio
async def test_summary_decimal_math_rounding_and_tcs():
    # 1,234,567.89 INR * 0.0117647059 = 14,524.3265... USD
    service, _ = _service([_transfer("1234567.89", "0.0117647059")])
    out = await service.get_lrs_summary(1, "2026-27")
    assert out["total_usd"] == "14524.33"
    assert out["total_inr"] == "1234567.89"
    assert out["tcs_threshold_inr"] == "1000000.00"
    assert out["tcs_applicable"] is True
    assert out["tcs_amount_inr"] == "46913.58"  # 20% of 234,567.89 = 46,913.578 -> half-up
    assert out["cap_remaining_usd"] == str(Decimal("250000.00") - Decimal("14524.33"))
    assert all(isinstance(out[k], str) for k in ("total_usd", "cap_usd", "pct_of_cap"))


@pytest.mark.asyncio
async def test_summary_transfer_without_fx_rate_is_flagged_not_dropped_from_inr():
    service, _ = _service([_transfer("100000.00", None, public_id="no-rate")])
    out = await service.get_lrs_summary(1, "2026-27")
    assert out["total_inr"] == "100000.00"
    assert out["total_usd"] == "0.00"
    assert out["unconverted_transfer_ids"] == ["no-rate"]
    assert any("could not be converted" in a for a in out["assumptions"])


@pytest.mark.asyncio
async def test_summary_non_usd_uses_ist_date_for_rate_lookup():
    fx_repo = MagicMock()
    fx_repo.get_latest_rate = AsyncMock(return_value=SimpleNamespace(rate=Decimal("1.25")))
    # 20:00 UTC on 15 Aug is already 16 Aug in IST.
    t = _transfer("100000", "0.0095", "GBP", datetime(2026, 8, 15, 20, 0, tzinfo=UTC))
    service, _ = _service([t, t], fx_repo)
    out = await service.get_lrs_summary(1, "2026-27")
    assert out["total_usd"] == "2375.00"  # 2 x 950 GBP x 1.25
    fx_repo.get_latest_rate.assert_awaited_once()  # deduplicated per (currency, IST date)
    kwargs = fx_repo.get_latest_rate.call_args.kwargs
    assert (kwargs["base_currency_code"], kwargs["quote_currency_code"]) == ("GBP", "USD")
    assert kwargs["as_of"].astimezone(IST).date() == date(2026, 8, 16)


@pytest.mark.asyncio
async def test_summary_old_fy_notes_indicative_tcs():
    service, _ = _service([])
    out = await service.get_lrs_summary(1, "2022-23")
    assert out["tcs_threshold_inr"] == "700000.00"
    assert any("indicative" in a for a in out["assumptions"])


# ------------------------------------------------------------ FX stored reads


def _tax_service(overrides):
    svc = TaxService(
        session=MagicMock(),
        order_repository=MagicMock(),
        reference_repository=MagicMock(),
        account_repository=MagicMock(),
        instrument_repository=MagicMock(),
    )
    profile = None if overrides is None else SimpleNamespace(fx_overrides=overrides)
    svc.profile_repository = MagicMock()
    svc.profile_repository.get = AsyncMock(return_value=profile)
    svc.profile_repository.get_or_create = AsyncMock(side_effect=AssertionError("must not write"))
    return svc


def test_normalize_currency_code():
    assert normalize_currency_code(" usd ") == "USD"
    for bad in ("", "US", "USDX", "U$D", "12A"):
        with pytest.raises(ValidationError):
            normalize_currency_code(bad)


@pytest.mark.asyncio
async def test_get_fx_rate_exact_key_only_and_no_profile():
    svc = _tax_service({"2026-01-31:USD": "86.50", "2026-01-31": "99", "2026-01": "98"})
    assert await svc.get_fx_rate(1, date(2026, 1, 31), "usd") == "86.50"
    assert await svc.get_fx_rate(1, date(2026, 1, 30), "USD") is None
    assert await svc.get_fx_rate(1, date(2026, 1, 31), "GBP") is None
    assert await _tax_service(None).get_fx_rate(1, date(2026, 1, 31), "USD") is None


@pytest.mark.asyncio
async def test_list_missing_fx_rates_edges():
    svc = _tax_service({"2026-01-02:USD": "86"})
    assert await svc.list_missing_fx_rates(1, date(2026, 1, 1), date(2026, 1, 3), "USD") == [
        date(2026, 1, 1),
        date(2026, 1, 3),
    ]
    assert await svc.list_missing_fx_rates(1, date(2026, 1, 2), date(2026, 1, 2), "USD") == []
    assert await _tax_service(None).list_missing_fx_rates(
        1, date(2026, 1, 1), date(2026, 1, 1), "USD"
    ) == [date(2026, 1, 1)]
    with pytest.raises(ValidationError):
        await svc.list_missing_fx_rates(1, date(2026, 1, 3), date(2026, 1, 1), "USD")
    with pytest.raises(ValidationError):
        await svc.list_missing_fx_rates(1, date(2026, 1, 1), date(2026, 1, 2), "US")
    assert MAX_FX_AUDIT_DAYS == 366
    with pytest.raises(ValidationError):
        await svc.list_missing_fx_rates(1, date(2026, 1, 1), date(2027, 1, 2), "USD")  # 367 days
