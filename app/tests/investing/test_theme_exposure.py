"""Theme tags, look-through exposure and allocation-vs-target (spec-105)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select, update

from app.config import settings
from app.core.database import postgres
from app.core.exceptions import ValidationError
from app.finance.models import FxRate
from app.investing.models import Company, HoldingThemeTag, InstrumentConstituent
from app.investing.theme_repository import LatestConstituentSnapshotRepository
from app.investing.theme_schemas import ThemeTagInput
from app.investing.theme_service import (
    ThemeExposureService,
    allocate_exact,
    validate_new_cash,
)
from app.mcp.server import create_mcp_server
from app.platform.models import Workspace
from app.tests.integration.test_investing import (
    _create_holding_via_order,
    _register_and_login,
    _submit_holding_price,
)

TODAY = datetime.now(UTC).date()
D = Decimal


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_allocate_exact_sums_to_rounded_total():
    raw = {"a": D("1") / D("3"), "b": D("1") / D("3"), "c": D("1") / D("3")}
    out = allocate_exact(raw, D("1"))
    assert sum(out.values()) == D("1.00")
    assert sorted(out.values()) == [D("0.33"), D("0.33"), D("0.34")]


def test_allocate_exact_handles_empty_and_zero():
    assert allocate_exact({}, D("0")) == {}
    assert allocate_exact({"a": D("0")}, D("0")) == {"a": D("0.00")}


@pytest.mark.parametrize("bad", [D("-1"), D("NaN"), D("Infinity"), D("10.005"), D("1e15")])
def test_validate_new_cash_rejects_bad_input(bad):
    with pytest.raises(ValidationError):
        validate_new_cash(bad)


def test_validate_new_cash_accepts_cents():
    assert validate_new_cash(D("12.5")) == D("12.50")
    assert validate_new_cash(D("0")) == D("0.00")


def test_plan_deployment_proportional_capped_and_exact():
    plan = ThemeExposureService._plan_deployment
    # Cash covers everything: capped at shortfall, rest left over.
    assert plan(D("1000"), {"a": D("100.004"), "b": D("50")}) == {
        "a": D("100.00"),
        "b": D("50.00"),
    }
    # Cash short: proportional, never above shortfall, sums to the cash.
    out = plan(D("100"), {"a": D("100"), "b": D("200")})
    assert out == {"a": D("33.33"), "b": D("66.67")}
    assert sum(out.values()) == D("100.00")
    assert plan(D("0"), {"a": D("5")}) == {}
    assert plan(D("5"), {"a": D("0")}) == {}


def test_theme_tag_input_normalises_theme():
    assert ThemeTagInput(theme="  AI   power/grid ").theme == "AI power/grid"
    with pytest.raises(ValueError):
        ThemeTagInput(theme="   ")
    with pytest.raises(ValueError):
        ThemeTagInput(theme="x", target_weight_pct=D("100.01"))
    with pytest.raises(ValueError):
        ThemeTagInput(theme="x", target_weight_pct=D("-1"))


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


async def _use_reporting_currency(client: AsyncClient, code: str) -> None:
    res = await client.patch("/v1/finance/settings", json={"reporting_currency_code": code})
    assert res.status_code == 200, res.text


async def _set_company(ticker: str, sector: str | None, country: str | None) -> None:
    async with postgres.async_session_maker() as session:
        await session.execute(
            update(Company)
            .where(Company.ticker == ticker)
            .values(sector=sector, country_code=country)
        )
        await session.commit()


async def _snapshot(client, instrument_id, source, rows, as_of=None, fetched_at=None):
    res = await client.post(
        f"/v1/investing/instruments/{instrument_id}/constituents",
        json={
            "as_of_date": (as_of or TODAY).isoformat(),
            "source": source,
            "fetched_at": (fetched_at or datetime.now(UTC)).isoformat(),
            "constituents": rows,
        },
    )
    assert res.status_code == 201, res.text


async def _seed(client: AsyncClient, email: str) -> dict[str, str]:
    """VTI ETF (1000, 60% AAPL / 30% MSFT, 10% uncovered), AAPL stock 200,
    OTHER-VTI placeholder 50. Reporting currency USD, so total = 1250."""
    accounts = await _register_and_login(
        client, email=f"{email}@example.com", username=email, password="TestPass123!"
    )
    await _use_reporting_currency(client, "USD")
    inst = await client.post(
        "/v1/investing/instruments",
        json={"symbol": "VTI", "name": "Vanguard Total Market ETF", "instrument_type": "etf"},
    )
    assert inst.status_code == 201, inst.text
    instrument_id = inst.json()["public_id"]
    await _snapshot(
        client,
        instrument_id,
        "src-new",
        [
            {"company_name": "Apple Inc", "company_ticker": "AAPL", "weight": "0.60000000"},
            {"company_name": "Microsoft Corp", "company_ticker": "MSFT", "weight": "0.30000000"},
            # How the snapshot writer stores "everything else": a placeholder row.
            {"company_name": "Other holdings", "company_ticker": "OTHER-VTI", "weight": "0.1"},
        ],
        fetched_at=datetime.now(UTC),
    )
    # A second, OLDER-fetched snapshot for the same date and an older-dated one:
    # neither may be added on top of the winning snapshot.
    await _snapshot(
        client,
        instrument_id,
        "src-old",
        [{"company_name": "Apple Inc", "company_ticker": "AAPL", "weight": "1.00000000"}],
        fetched_at=datetime.now(UTC) - timedelta(hours=5),
    )
    await _snapshot(
        client,
        instrument_id,
        "src-ancient",
        [{"company_name": "Tesla Inc", "company_ticker": "TSLA", "weight": "1.00000000"}],
        as_of=TODAY - timedelta(days=30),
    )
    vti = await _create_holding_via_order(
        client, accounts["brokerage"], "VTI", "10.00000000", "100.00", "USD", "etf"
    )
    aapl = await _create_holding_via_order(
        client, accounts["wallet"], "AAPL", "2.00000000", "100.00", "USD"
    )
    other = await _create_holding_via_order(
        client, accounts["primary"], "OTHER-VTI", "50.00000000", "1.00", "USD"
    )
    await _submit_holding_price(client, vti, "100.00", TODAY.isoformat())
    await _submit_holding_price(client, aapl, "100.00", TODAY.isoformat())
    await _submit_holding_price(client, other, "1.00", TODAY.isoformat())
    await _set_company("AAPL", "Technology", "US")
    await _set_company("MSFT", "Technology", "US")
    await _set_company("TSLA", "Consumer", "US")
    return {"vti": vti, "aapl": aapl, "other": other}


async def _tag(client: AsyncClient, holdings: list[dict]) -> Response:
    return await client.put("/v1/investing/holdings/theme-tags", json={"holdings": holdings})


def _bucket(rows: list[dict], key: str) -> Decimal:
    return D(next(r["value"] for r in rows if r["key"] == key))


# ---------------------------------------------------------------------------
# Look-through exposure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookthrough_rolls_up_company_sector_country_theme(client: AsyncClient):
    ids = await _seed(client, "lt-rollup")
    res = await _tag(
        client,
        [
            {"holding_id": ids["vti"], "tags": [{"theme": "Core", "target_weight_pct": "30"}]},
            {"holding_id": ids["aapl"], "tags": [{"theme": "Tech"}]},
        ],
    )
    assert res.status_code == 200, res.text

    res = await client.get(
        "/v1/investing/analytics/lookthrough-exposure", params={"as_of": TODAY.isoformat()}
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["currency"] == "USD"
    assert D(data["total_value"]) == D("1250.00")
    # One snapshot only: 10% of the ETF (100) is uncovered, the placeholder (50) too.
    assert D(data["not_itemised_value"]) == D("150.00")
    assert D(data["itemised_value"]) == D("1100.00")
    assert D(data["not_itemised_pct"]) == D("12.00")

    # Sector/country partitions sum EXACTLY to the total, not-itemised included.
    for key in ("by_sector", "by_country"):
        assert sum(D(r["value"]) for r in data[key]) == D("1250.00")
    assert _bucket(data["by_sector"], "Technology") == D("1100.00")
    assert _bucket(data["by_sector"], "not_itemised") == D("150.00")
    assert "Consumer" not in {r["key"] for r in data["by_sector"]}  # TSLA snapshot ignored
    assert _bucket(data["by_country"], "US") == D("1100.00")

    # Companies: AAPL = 600 look-through (+200 direct, possibly same row), MSFT 300.
    by_ticker: dict[str, Decimal] = {}
    for row in data["by_company"]:
        by_ticker[row["company_ticker"]] = by_ticker.get(row["company_ticker"], D(0)) + D(
            row["total_value"]
        )
    assert by_ticker["AAPL"] == D("800.00")
    assert by_ticker["MSFT"] == D("300.00")
    assert "TSLA" not in by_ticker
    assert sum(by_ticker.values()) + D(data["not_itemised_value"]) == D(data["total_value"])

    themes = {r["theme"]: r for r in data["by_theme"]}
    assert D(themes["Core"]["value"]) == D("1000.00")
    assert D(themes["Core"]["pct"]) == D("80.00")
    assert D(themes["Tech"]["value"]) == D("200.00")
    assert D(data["unthemed_value"]) == D("50.00")

    cov = data["fund_coverage"][0]
    assert cov["source"] == "src-new"
    assert D(cov["itemised_pct"]) == D("90.00")
    assert cov["constituent_rows"] == 3


@pytest.mark.asyncio
async def test_lookthrough_group_by_limits_sections(client: AsyncClient):
    await _seed(client, "lt-groupby")
    res = await client.get(
        "/v1/investing/analytics/lookthrough-exposure", params={"group_by": "sector"}
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["by_sector"] is not None
    assert data["by_company"] is None
    assert data["by_country"] is None
    assert data["by_theme"] is None

    bad = await client.get(
        "/v1/investing/analytics/lookthrough-exposure", params={"group_by": "colour"}
    )
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_lookthrough_empty_portfolio_has_no_division_by_zero(client: AsyncClient):
    await _register_and_login(
        client, email="lt-empty@example.com", username="lt-empty", password="TestPass123!"
    )
    res = await client.get("/v1/investing/analytics/lookthrough-exposure")
    assert res.status_code == 200, res.text
    data = res.json()
    assert D(data["total_value"]) == 0
    assert D(data["not_itemised_pct"]) == 0
    assert data["by_company"] == []
    assert data["by_theme"] == []
    assert D(data["unthemed_value"]) == 0
    assert data["analysis_status"] == "complete"


@pytest.mark.asyncio
async def test_lookthrough_missing_fx_rate_is_excluded_with_warning(client: AsyncClient):
    accounts = await _register_and_login(
        client, email="lt-nofx@example.com", username="lt-nofx", password="TestPass123!"
    )
    await _use_reporting_currency(client, "INR")
    h = await _create_holding_via_order(
        client, accounts["brokerage"], "AAPL", "2.00000000", "100.00", "USD"
    )
    await _submit_holding_price(client, h, "100.00", TODAY.isoformat())
    res = await client.get("/v1/investing/analytics/lookthrough-exposure")
    data = res.json()
    assert res.status_code == 200, res.text
    assert D(data["total_value"]) == 0
    assert data["analysis_status"] == "partial"
    assert any("no FX rate USD->INR" in w for w in data["warnings"]), data["warnings"]


@pytest.mark.asyncio
async def test_lookthrough_converts_to_reporting_currency(client: AsyncClient):
    accounts = await _register_and_login(
        client, email="lt-fx@example.com", username="lt-fx", password="TestPass123!"
    )
    await _use_reporting_currency(client, "INR")
    h = await _create_holding_via_order(
        client, accounts["brokerage"], "AAPL", "2.00000000", "100.00", "USD"
    )
    await _submit_holding_price(client, h, "100.00", TODAY.isoformat())
    async with postgres.async_session_maker() as session:
        session.add(
            FxRate(
                base_currency_code="USD",
                quote_currency_code="INR",
                rate=D("80"),
                as_of=datetime.now(UTC) - timedelta(days=1),
                fetched_at=datetime.now(UTC),
                source="test",
            )
        )
        await session.commit()
    data = (await client.get("/v1/investing/analytics/lookthrough-exposure")).json()
    assert data["currency"] == "INR"
    assert D(data["total_value"]) == D("16000.00")
    assert D(data["fx_rates_used"]["USD"]) == D("80")
    assert data["warnings"] == []


# ---------------------------------------------------------------------------
# Theme tags
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_theme_tags_set_replace_clear_and_idempotent(client: AsyncClient):
    ids = await _seed(client, "tags-crud")
    body = [
        {
            "holding_id": ids["vti"],
            "tags": [
                {"theme": "AI power/grid", "target_weight_pct": "10"},
                {"theme": "Battery", "target_weight_pct": "5.5"},
            ],
        }
    ]
    first = await _tag(client, body)
    assert first.status_code == 200, first.text
    first_data = first.json()
    assert {t["theme"] for t in first_data["tags"]} == {"AI power/grid", "Battery"}
    assert D(first_data["total_target_pct"]) == D("15.5")
    assert D(first_data["unallocated_target_pct"]) == D("84.5")

    # Re-sending the same set is idempotent and keeps tag identities.
    again = (await _tag(client, body)).json()
    assert {t["public_id"] for t in again["tags"]} == {t["public_id"] for t in first_data["tags"]}

    # Changing a target updates in place; dropping a theme removes it.
    changed = (
        await _tag(
            client,
            [{"holding_id": ids["vti"], "tags": [{"theme": "battery", "target_weight_pct": "7"}]}],
        )
    ).json()
    assert [(t["theme"], D(t["target_weight_pct"])) for t in changed["tags"]] == [
        ("battery", D("7"))  # explicit re-spelling of the holding's only tag wins
    ]
    got = (await client.get(f"/v1/investing/holdings/{ids['vti']}/theme-tags")).json()
    assert [t["theme"] for t in got["tags"]] == ["battery"]

    cleared = await client.put(f"/v1/investing/holdings/{ids['vti']}/theme-tags", json={"tags": []})
    assert cleared.status_code == 200
    assert cleared.json()["tags"] == []
    async with postgres.async_session_maker() as session:
        count = (await session.execute(select(func.count()).select_from(HoldingThemeTag))).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_theme_tag_theme_spelling_is_canonical_across_holdings(client: AsyncClient):
    ids = await _seed(client, "tags-case")
    await _tag(client, [{"holding_id": ids["vti"], "tags": [{"theme": "Robotics"}]}])
    res = (
        await _tag(client, [{"holding_id": ids["aapl"], "tags": [{"theme": "  robotics "}]}])
    ).json()
    assert res["tags"][0]["theme"] == "Robotics"
    assert len(res["theme_targets"]) == 1
    assert res["theme_targets"][0]["holdings_count"] == 2
    assert res["theme_targets"][0]["target_weight_pct"] is None


@pytest.mark.asyncio
async def test_theme_targets_over_100_rejected_and_nothing_written(client: AsyncClient):
    ids = await _seed(client, "tags-over")
    ok = await _tag(
        client,
        [{"holding_id": ids["vti"], "tags": [{"theme": "A", "target_weight_pct": "60"}]}],
    )
    assert ok.status_code == 200
    over = await _tag(
        client,
        [
            {"holding_id": ids["aapl"], "tags": [{"theme": "B", "target_weight_pct": "30"}]},
            {"holding_id": ids["other"], "tags": [{"theme": "C", "target_weight_pct": "10.01"}]},
        ],
    )
    assert over.status_code == 422, over.text
    assert "100%" in over.text
    async with postgres.async_session_maker() as session:
        themes = (await session.execute(select(HoldingThemeTag.theme))).scalars().all()
    assert themes == ["A"]  # the failed batch wrote nothing (atomic)
    # Exactly 100 is fine.
    full = await _tag(
        client,
        [{"holding_id": ids["aapl"], "tags": [{"theme": "B", "target_weight_pct": "40"}]}],
    )
    assert full.status_code == 200, full.text


@pytest.mark.asyncio
async def test_theme_tag_validation_errors(client: AsyncClient):
    ids = await _seed(client, "tags-invalid")
    dup = await _tag(
        client,
        [{"holding_id": ids["vti"], "tags": [{"theme": "AI"}, {"theme": "ai"}]}],
    )
    assert dup.status_code == 422
    dup_holding = await _tag(
        client,
        [
            {"holding_id": ids["vti"], "tags": [{"theme": "AI"}]},
            {"holding_id": ids["vti"], "tags": [{"theme": "AI"}]},
        ],
    )
    assert dup_holding.status_code == 422
    assert (
        await _tag(client, [{"holding_id": ids["vti"], "tags": [{"theme": " "}]}])
    ).status_code == 422
    assert (
        await _tag(
            client,
            [{"holding_id": ids["vti"], "tags": [{"theme": "X", "target_weight_pct": "101"}]}],
        )
    ).status_code == 422
    assert (await _tag(client, [])).status_code == 422
    unknown = await _tag(client, [{"holding_id": str(uuid.uuid4()), "tags": []}])
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_theme_tags_are_workspace_isolated(client: AsyncClient):
    ids = await _seed(client, "iso-a")
    await _tag(
        client,
        [{"holding_id": ids["vti"], "tags": [{"theme": "Secret", "target_weight_pct": "50"}]}],
    )
    await client.post("/v1/auth/logout")
    await _register_and_login(
        client, email="iso-b@example.com", username="iso-b", password="TestPass123!"
    )
    # B cannot read or write A's holding tags.
    assert (await client.get(f"/v1/investing/holdings/{ids['vti']}/theme-tags")).status_code == 404
    put = await client.put(
        f"/v1/investing/holdings/{ids['vti']}/theme-tags",
        json={"tags": [{"theme": "Hijack"}]},
    )
    assert put.status_code == 404
    assert (await _tag(client, [{"holding_id": ids["vti"], "tags": []}])).status_code == 404
    # B sees none of A's themes or targets in analytics.
    exposure = (await client.get("/v1/investing/analytics/lookthrough-exposure")).json()
    assert exposure["by_theme"] == []
    alloc = (
        await client.get("/v1/investing/analytics/allocation-vs-target", params={"new_cash": "100"})
    ).json()
    assert alloc["themes"] == []
    assert D(alloc["unallocated_cash"]) == D("100.00")
    # And A's tag is untouched.
    async with postgres.async_session_maker() as session:
        themes = (await session.execute(select(HoldingThemeTag.theme))).scalars().all()
    assert themes == ["Secret"]


# ---------------------------------------------------------------------------
# Allocation vs target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allocation_vs_target_drift_and_proportional_deployment(client: AsyncClient):
    ids = await _seed(client, "alloc-main")
    res = await _tag(
        client,
        [
            {"holding_id": ids["vti"], "tags": [{"theme": "Core", "target_weight_pct": "30"}]},
            {"holding_id": ids["aapl"], "tags": [{"theme": "Tech", "target_weight_pct": "40"}]},
            {"holding_id": ids["other"], "tags": [{"theme": "Misc", "target_weight_pct": "20"}]},
        ],
    )
    assert res.status_code == 200, res.text

    res = await client.get(
        "/v1/investing/analytics/allocation-vs-target",
        params={"new_cash": "750", "as_of": TODAY.isoformat()},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert D(data["portfolio_value"]) == D("1250.00")
    assert D(data["post_deployment_value"]) == D("2000.00")
    assert D(data["total_target_pct"]) == D("90")
    assert D(data["unallocated_target_pct"]) == D("10")
    themes = {t["theme"]: t for t in data["themes"]}
    # drift = actual% - target%
    assert D(themes["Core"]["current_pct"]) == D("80.00")
    assert D(themes["Core"]["drift_pct"]) == D("50.00")
    assert D(themes["Tech"]["drift_pct"]) == D("-24.00")
    assert D(themes["Misc"]["drift_pct"]) == D("-16.00")
    # Shortfalls are measured against portfolio + new cash (2000).
    assert D(themes["Tech"]["shortfall"]) == D("600.00")
    assert D(themes["Misc"]["shortfall"]) == D("350.00")
    assert D(themes["Core"]["shortfall"]) == 0
    # Cash (750) < total shortfall (950): proportional, never a sell, sums exactly.
    assert D(themes["Core"]["suggested_new_cash"]) == 0
    assert D(themes["Tech"]["suggested_new_cash"]) == D("473.68")
    assert D(themes["Misc"]["suggested_new_cash"]) == D("276.32")
    assert D(data["total_suggested"]) == D("750.00")
    assert D(data["unallocated_cash"]) == D("0.00")
    assert all(D(t["suggested_new_cash"]) >= 0 for t in data["themes"])
    # Most under-weight first.
    assert [t["theme"] for t in data["themes"]] == ["Tech", "Misc", "Core"]
    assert themes["Tech"]["holdings"][0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_allocation_caps_at_shortfall_and_reports_leftover(client: AsyncClient):
    ids = await _seed(client, "alloc-cap")
    await _tag(
        client,
        [
            {"holding_id": ids["vti"], "tags": [{"theme": "Core", "target_weight_pct": "30"}]},
            {"holding_id": ids["aapl"], "tags": [{"theme": "Tech", "target_weight_pct": "40"}]},
        ],
    )
    # new_cash 750 -> T=2000, Tech shortfall 600, Core over target: 150 is left over.
    data = (
        await client.get("/v1/investing/analytics/allocation-vs-target", params={"new_cash": "750"})
    ).json()
    themes = {t["theme"]: t for t in data["themes"]}
    assert D(themes["Tech"]["suggested_new_cash"]) == D("600.00")
    assert D(themes["Core"]["suggested_new_cash"]) == 0
    assert D(data["total_suggested"]) == D("600.00")
    assert D(data["unallocated_cash"]) == D("150.00")
    assert D(data["total_suggested"]) + D(data["unallocated_cash"]) == D(data["new_cash"])
    # Without new cash nothing is suggested - and nothing is ever a sell.
    zero = (await client.get("/v1/investing/analytics/allocation-vs-target")).json()
    assert D(zero["total_suggested"]) == 0
    assert all(D(t["suggested_new_cash"]) == 0 for t in zero["themes"])
    assert D({t["theme"]: t for t in zero["themes"]}["Core"]["drift_pct"]) == D("50.00")


@pytest.mark.asyncio
async def test_allocation_tag_without_target_gets_no_deployment(client: AsyncClient):
    ids = await _seed(client, "alloc-notarget")
    await _tag(client, [{"holding_id": ids["vti"], "tags": [{"theme": "Watch"}]}])
    data = (
        await client.get("/v1/investing/analytics/allocation-vs-target", params={"new_cash": "500"})
    ).json()
    row = data["themes"][0]
    assert row["target_weight_pct"] is None
    assert row["drift_pct"] is None
    assert D(row["suggested_new_cash"]) == 0
    assert D(data["unallocated_cash"]) == D("500.00")


@pytest.mark.asyncio
async def test_allocation_zero_total_value_and_empty_portfolio(client: AsyncClient):
    accounts = await _register_and_login(
        client, email="alloc-zero@example.com", username="alloc-zero", password="TestPass123!"
    )
    await _use_reporting_currency(client, "USD")
    # Empty portfolio, no tags: all cash unallocated.
    empty = (
        await client.get("/v1/investing/analytics/allocation-vs-target", params={"new_cash": "100"})
    ).json()
    assert empty["themes"] == []
    assert D(empty["unallocated_cash"]) == D("100.00")
    assert D(empty["portfolio_value"]) == 0

    # A held position with no price has zero valued total; targets still work.
    h = await _create_holding_via_order(
        client, accounts["brokerage"], "AAPL", "2.00000000", "100.00", "USD"
    )
    await _tag(client, [{"holding_id": h, "tags": [{"theme": "Tech", "target_weight_pct": "40"}]}])
    none_cash = await client.get("/v1/investing/analytics/allocation-vs-target")
    assert none_cash.status_code == 200, none_cash.text
    assert D(none_cash.json()["portfolio_value"]) == 0
    assert D(none_cash.json()["themes"][0]["current_pct"]) == 0
    assert D(none_cash.json()["themes"][0]["post_deployment_pct"]) == 0
    funded = (
        await client.get("/v1/investing/analytics/allocation-vs-target", params={"new_cash": "100"})
    ).json()
    assert D(funded["themes"][0]["suggested_new_cash"]) == D("40.00")
    assert D(funded["unallocated_cash"]) == D("60.00")
    assert funded["analysis_status"] == "partial"  # unpriced holding is reported, not hidden
    assert any("no market price" in w for w in funded["warnings"])


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["-5", "abc", "10.005", "NaN", "Infinity"])
async def test_allocation_rejects_invalid_new_cash(client: AsyncClient, bad: str):
    await _register_and_login(
        client, email="alloc-bad@example.com", username="alloc-bad", password="TestPass123!"
    )
    res = await client.get("/v1/investing/analytics/allocation-vs-target", params={"new_cash": bad})
    assert res.status_code == 422, res.text


# ---------------------------------------------------------------------------
# Repository: exactly one snapshot per instrument
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_snapshot_tie_breaks_on_fetched_at_then_date(client: AsyncClient):
    await _seed(client, "snap-single")
    async with postgres.async_session_maker() as session:
        instrument_ids = (
            (await session.execute(select(InstrumentConstituent.instrument_id).distinct()))
            .scalars()
            .all()
        )
        repo = LatestConstituentSnapshotRepository(session)
        got = await repo.get_single_latest_many(instrument_ids, TODAY)
        assert len(got) == 1
        rows = next(iter(got.values()))
        assert {r.source for r in rows} == {"src-new"}
        assert {r.as_of_date for r in rows} == {TODAY}
        # Before the newest snapshot existed we fall back to the older-dated one.
        earlier = await repo.get_single_latest_many(instrument_ids, TODAY - timedelta(days=1))
        assert {r.source for r in next(iter(earlier.values()))} == {"src-ancient"}
        assert await repo.get_single_latest_many([], TODAY) == {}
        assert await repo.get_single_latest_many(instrument_ids, TODAY - timedelta(days=90)) == {}


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


async def _tool_map():
    tools = await create_mcp_server().list_tools()
    return {t.name: t.fn for t in tools}


@pytest.mark.asyncio
async def test_mcp_theme_tools_end_to_end_with_scopes(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.lifestack.test")
    ids = await _seed(client, "mcp-themes")
    async with postgres.async_session_maker() as session:
        workspace_id = (await session.execute(select(Workspace.id))).scalars().first()
    auth = AsyncMock(return_value=1)
    monkeypatch.setattr("app.mcp.server.authorize_workspace", auth)
    tools = await _tool_map()

    payload = [
        {
            "holding_id": ids["aapl"],
            "tags": [{"theme": "Tech", "target_weight_pct": "40"}],
        }
    ]
    preview = await tools["set_holding_theme_tags"](workspace_id=workspace_id, holdings=payload)
    assert preview["status"] == "needs_confirmation"
    assert auth.await_count == 0  # nothing authorised/written before confirmation
    written = await tools["set_holding_theme_tags"](
        workspace_id=workspace_id, holdings=payload, confirmed=True
    )
    assert written["status"] == "success", written
    assert written["tags"][0]["theme"] == "Tech"
    assert auth.await_args.kwargs["required_scope"] == "mcp:research"

    bad = await tools["set_holding_theme_tags"](
        workspace_id=workspace_id,
        holdings=[
            {"holding_id": ids["aapl"], "tags": [{"theme": "X", "target_weight_pct": "101"}]}
        ],
        confirmed=True,
    )
    assert bad["status"] == "error"
    over = await tools["set_holding_theme_tags"](
        workspace_id=workspace_id,
        holdings=[{"holding_id": ids["vti"], "tags": [{"theme": "Y", "target_weight_pct": "61"}]}],
        confirmed=True,
    )
    assert over["status"] == "error"
    assert "100%" in over["message"]

    exposure = await tools["get_lookthrough_exposure"](
        workspace_id=workspace_id, as_of_date=TODAY.isoformat(), group_by="theme"
    )
    assert exposure["status"] == "success"
    assert auth.await_args.kwargs["required_scope"] == "mcp:read"
    assert exposure["by_theme"][0]["theme"] == "Tech"
    assert exposure["by_sector"] is None
    assert (await tools["get_lookthrough_exposure"](workspace_id=1, group_by="x"))[
        "status"
    ] == "error"
    assert (await tools["get_lookthrough_exposure"](workspace_id=1, as_of_date="nope"))[
        "status"
    ] == "error"

    alloc = await tools["get_allocation_vs_target"](
        workspace_id=workspace_id, new_cash="1000", as_of_date=TODAY.isoformat()
    )
    assert alloc["status"] == "success"
    assert auth.await_args.kwargs["required_scope"] == "mcp:read"
    assert D(alloc["themes"][0]["suggested_new_cash"]) == D("1000.00") - D(
        alloc["unallocated_cash"]
    )
    assert (await tools["get_allocation_vs_target"](workspace_id=1, new_cash="abc"))[
        "status"
    ] == "error"
    assert (await tools["get_allocation_vs_target"](workspace_id=1, new_cash="-1"))[
        "status"
    ] == "error"
