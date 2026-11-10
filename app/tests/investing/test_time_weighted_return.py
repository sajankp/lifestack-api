"""Snapshot-linked time-weighted return (spec-103)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.investing.performance_service import PerformanceService


def _snap(day: int, value: str, currency: str = "INR", fx: dict | None = None):
    return SimpleNamespace(
        snapshot_date=date(2026, 1, day),
        total_value=Decimal(value),
        currency_code=currency,
        fx_rates_used=fx or {},
    )


def _transfer(
    day: int,
    *,
    to_investing: bool = False,
    from_investing: bool = False,
    amount: str = "0",
    gross: str | None = None,
    to_ccy: str = "INR",
    from_ccy: str = "INR",
):
    return SimpleNamespace(
        occurred_at=datetime(2026, 1, day, 12, 0, tzinfo=UTC),
        to_module="investing" if to_investing else "spending",
        from_module="investing" if from_investing else "spending",
        net_amount_received=Decimal(amount),
        gross_amount=Decimal(gross if gross is not None else amount),
        to_currency_code=to_ccy,
        from_currency_code=from_ccy,
    )


def _service(snapshots, transfers=None, with_transfer_repo=True) -> PerformanceService:
    snapshot_repo = MagicMock()
    snapshot_repo.list_range = AsyncMock(return_value=snapshots)
    transfer_repo = None
    if with_transfer_repo:
        transfer_repo = MagicMock()
        transfer_repo.list_investing_flows_in_period = AsyncMock(return_value=transfers or [])
    return PerformanceService(
        MagicMock(), MagicMock(), MagicMock(), snapshot_repo, transfer_repo=transfer_repo
    )


async def _twr(service: PerformanceService, from_date=None, to_date=date(2026, 1, 31)):
    return await service.get_time_weighted_return(1, from_date, to_date)


@pytest.mark.anyio
async def test_chain_links_sub_period_returns():
    result = await _twr(_service([_snap(1, "100"), _snap(11, "110"), _snap(21, "121")]))
    assert result["status"] == "ok"
    assert result["twr_pct"] == "21.00"
    assert result["periods"] == 2
    assert result["method"] == "modified_dietz_chain_linked"
    assert result["net_external_flows"] == "0.00"
    assert result["start_date"] == "2026-01-01" and result["end_date"] == "2026-01-21"


@pytest.mark.anyio
async def test_contribution_does_not_inflate_the_return():
    # Value doubled only because 1000 was deposited on the snapshot day.
    service = _service(
        [_snap(1, "1000"), _snap(11, "2100")], [_transfer(11, to_investing=True, amount="1000")]
    )
    result = await _twr(service)
    assert result["twr_pct"] == "10.00"  # not 110%
    assert result["net_external_flows"] == "1000.00"


@pytest.mark.anyio
async def test_mid_period_flow_is_day_weighted():
    # Deposit 1000 on day 6 of a 10-day period: weight 0.5.
    # r = (2200 - 1000 - 1000) / (1000 + 0.5 * 1000) = 200 / 1500
    service = _service(
        [_snap(1, "1000"), _snap(11, "2200")], [_transfer(6, to_investing=True, amount="1000")]
    )
    assert (await _twr(service))["twr_pct"] == "13.33"


@pytest.mark.anyio
async def test_withdrawal_is_neutralised():
    service = _service(
        [_snap(1, "2000"), _snap(11, "1155")],
        [_transfer(11, from_investing=True, amount="900", gross="1000")],
    )
    # r = (1155 - 2000 + 1000) / 2000 = 7.75%
    result = await _twr(service)
    assert result["twr_pct"] == "7.75"
    assert result["net_external_flows"] == "-1000.00"


@pytest.mark.anyio
async def test_transfers_not_touching_investing_are_ignored():
    service = _service([_snap(1, "100"), _snap(11, "110")], [_transfer(5, amount="500")])
    assert (await _twr(service))["twr_pct"] == "10.00"


@pytest.mark.anyio
async def test_foreign_currency_transfer_uses_snapshot_fx():
    # 10 USD deposited = 830 INR at the later snapshot's stored rate.
    service = _service(
        [_snap(1, "1000"), _snap(11, "1913", fx={"USD": "83"})],
        [_transfer(11, to_investing=True, amount="10", to_ccy="USD")],
    )
    result = await _twr(service)
    assert result["status"] == "ok"
    assert result["net_external_flows"] == "830.00"
    assert result["twr_pct"] == "8.30"  # (1913 - 1000 - 830) / 1000


@pytest.mark.anyio
async def test_missing_fx_for_a_transfer_yields_status_not_a_guess():
    service = _service(
        [_snap(1, "1000"), _snap(11, "1913")],
        [_transfer(11, to_investing=True, amount="10", to_ccy="USD")],
    )
    result = await _twr(service)
    assert result["status"] == "missing_fx_for_flows"
    assert result["twr_pct"] is None
    assert any("USD" in w for w in result["warnings"])


@pytest.mark.anyio
@pytest.mark.parametrize("snapshots", [[], [_snap(1, "100")]])
async def test_fewer_than_two_snapshots(snapshots):
    result = await _twr(_service(snapshots))
    assert result["status"] == "insufficient_snapshots"
    assert result["twr_pct"] is None
    assert result["periods"] == 0
    assert result["warnings"]


@pytest.mark.anyio
async def test_mixed_snapshot_currency_is_not_linked():
    result = await _twr(_service([_snap(1, "100", "INR"), _snap(11, "110", "USD")]))
    assert result["status"] == "mixed_snapshot_currency"
    assert result["twr_pct"] is None


@pytest.mark.anyio
async def test_zero_opening_value_periods_are_skipped_not_divided():
    result = await _twr(_service([_snap(1, "0"), _snap(11, "100"), _snap(21, "110")]))
    assert result["status"] == "ok"
    assert result["periods"] == 1
    assert result["periods_skipped"] == 1
    assert result["twr_pct"] == "10.00"


@pytest.mark.anyio
async def test_all_periods_uncomputable():
    result = await _twr(_service([_snap(1, "0"), _snap(11, "0")]))
    assert result["status"] == "no_computable_periods"
    assert result["twr_pct"] is None


@pytest.mark.anyio
async def test_zero_closing_value_period_is_skipped_not_compounded():
    # A zero-value snapshot mid-chain must not force the whole chain to -100%;
    # the period is skipped and the computable sub-periods are linked instead.
    result = await _twr(
        _service([_snap(1, "100"), _snap(8, "0"), _snap(15, "110"), _snap(22, "121")])
    )
    assert result["status"] == "ok"
    assert result["periods"] == 1
    assert result["periods_skipped"] == 2
    assert result["twr_pct"] == "10.00"


@pytest.mark.anyio
async def test_without_transfer_repo_result_is_flagged_unadjusted():
    result = await _twr(_service([_snap(1, "100"), _snap(11, "110")], with_transfer_repo=False))
    assert result["twr_pct"] == "10.00"
    assert any("not adjusted" in w for w in result["warnings"])


@pytest.mark.anyio
async def test_window_is_passed_to_the_repositories():
    service = _service([_snap(1, "100"), _snap(11, "110")])
    await service.get_time_weighted_return(7, date(2026, 1, 1), date(2026, 1, 31))
    service.snapshot_repo.list_range.assert_awaited_once_with(
        7, date(2026, 1, 1), date(2026, 1, 31)
    )
    args = service.transfer_repo.list_investing_flows_in_period.await_args.args
    assert args[0] == 7
    # Transfers strictly after the first snapshot day up to the last snapshot day.
    assert args[1] == datetime(2026, 1, 2, tzinfo=UTC)
    assert args[2] == datetime(2026, 1, 12, tzinfo=UTC)
