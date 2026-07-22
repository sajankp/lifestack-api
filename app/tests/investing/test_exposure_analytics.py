from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient

from app.tests.integration.test_investing import (
    _create_holding_via_order,
    _register_and_login,
)


@pytest.mark.asyncio
async def test_exposure_coverage_is_value_weighted_not_count_weighted(client: AsyncClient):
    """spec-085: a large, fully-resolved stock position alongside one small
    fund position with no constituent snapshot must not report near-zero
    coverage just because the fund (1 of 2 positions) failed to decompose.
    Coverage should reflect how much portfolio VALUE is resolved, matching
    what the exposure chart actually shows (the stock, in full)."""
    account_map = await _register_and_login(
        client,
        email="exposure-coverage@example.com",
        username="exposurecoverage",
        password="TestPass123!",
    )
    # Large, fully-resolvable stock position: 100 * 100 = 10,000.
    await _create_holding_via_order(
        client, account_map["brokerage"], "AAPL", "100.00000000", "100.00"
    )
    # Small fund position with no constituent snapshot ingested: 1 * 10 = 10.
    await _create_holding_via_order(
        client,
        account_map["brokerage"],
        "TESTFUND",
        "1.00000000",
        "10.00",
        instrument_type="mutual_fund",
        instrument_name="Test Fund With No Constituents",
    )

    res = await client.get(
        "/v1/investing/analytics/exposure", params={"as_of": date.today().isoformat()}
    )
    assert res.status_code == 200, res.text
    data = res.json()

    coverage = Decimal(str(data["snapshot_coverage"]))
    # Old (count-based) behavior would be exactly 0 (0 of 1 funds decomposed).
    # Value-weighted: ~10,000 / 10,010 resolved -- overwhelmingly covered.
    assert coverage > Decimal("0.9"), (
        f"coverage should be value-weighted (~0.999), got {coverage} -- "
        "looks count-weighted (0 of 1 funds decomposed)"
    )
    assert data["analysis_status"] == "partial"
    assert any("No constituent snapshot" in w for w in data["warnings"])
    # The stock's direct exposure is still fully present in the response,
    # matching what the chart renders -- coverage near-zero here would have
    # read as an internal contradiction against this real data.
    assert Decimal(str(data["total_direct_exposure"])) == Decimal("10000")
