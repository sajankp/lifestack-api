"""Halal compliance verdicts + purification estimates (spec-104).

DB-backed tests exercising set/get compliance per holding with cited
sources, purification estimates for dividends and holdings, workspace
isolation, validation, DB constraints and the MCP tools.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.core.audit import AuditLog
from app.core.database import postgres
from app.investing.models import Holding, HoldingCompliance
from app.investing.theme_repository import HoldingThemeTagRepository
from app.mcp.server import create_mcp_server
from app.tests.integration.test_investing import (
    _create_holding_via_order,
    _register_and_login,
)

_COMPLIANCE_URL = "/v1/investing/holdings/{}/compliance"
_ESTIMATE_URL = "/v1/investing/purification/estimate"
_AS_OF = date(2026, 11, 1).isoformat()


@pytest.fixture
def mcp_enabled(monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.lifestack.test")


def _source(**overrides) -> dict:
    src = {
        "screener_name": "Example Halal Screener",
        "url": "https://example.com/screener/aapl",
        "rule_reference": "AAOIFI SS 21: non-permissible income < 5% of revenue",
    }
    src.update(overrides)
    return src


def _payload(**overrides) -> dict:
    body: dict = {
        "verdict": "permissible",
        "sources": [_source()],
        "as_of_date": _AS_OF,
        "impure_income_pct": "10.0000",
    }
    body.update(overrides)
    return body


async def _set_compliance(client: AsyncClient, holding_id: str, **overrides) -> dict:
    res = await client.put(_COMPLIANCE_URL.format(holding_id), json=_payload(**overrides))
    assert res.status_code == 200, res.text
    return res.json()


async def _holding(client: AsyncClient, email: str, symbol: str = "TLTM") -> tuple[dict, str]:
    account_map = await _register_and_login(
        client, email=f"{email}@example.com", username=email, password="TestPass123!"
    )
    holding_id = await _create_holding_via_order(
        client, account_map["brokerage"], symbol, "10.00000000", "100.00", "USD"
    )
    return account_map, holding_id


async def _create_dividend(
    client: AsyncClient,
    account_id: str,
    symbol: str,
    gross: str,
    tax: str = "0.00",
    pay_date: str = "2026-10-01",
) -> str:
    res = await client.post(
        "/v1/investing/dividends",
        json={
            "account_id": account_id,
            "symbol": symbol,
            "gross_amount": gross,
            "tax_withheld": tax,
            "currency": "USD",
            "pay_date": pay_date,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["public_id"]


# ---------------------------------------------------------------------------
# REST: set / get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_get_roundtrip_with_cited_sources(client: AsyncClient):
    _, holding_id = await _holding(client, "cmp-roundtrip", "AAPL")
    body = await _set_compliance(
        client,
        holding_id,
        sources=[
            _source(verdict="halal", as_of_date="2026-10-15"),
            _source(
                screener_name="Zoya",
                url="http://zoya.example/aapl",
                rule_reference="Debt/market cap < 30%",
                verdict="permissible",
            ),
        ],
    )
    assert body["holding_public_id"] == holding_id
    assert body["verdict"] == "permissible"
    assert body["impure_income_pct"] == "10.0000"
    assert body["as_of_date"] == _AS_OF
    # Screeners disagree: both citations stay visible and the disagreement is flagged.
    assert body["sources_disagree"] is True
    assert [s["screener_name"] for s in body["sources"]] == ["Example Halal Screener", "Zoya"]
    assert body["sources"][0]["verdict"] == "halal"
    assert body["sources"][0]["as_of_date"] == "2026-10-15"
    assert body["sources"][1]["url"] == "http://zoya.example/aapl"

    got = (await client.get(_COMPLIANCE_URL.format(holding_id))).json()
    assert got["sources"] == body["sources"]
    assert got["public_id"] == body["public_id"]

    async with postgres.async_session_maker() as session:
        row = (
            await session.execute(
                select(HoldingCompliance).where(
                    HoldingCompliance.public_id == UUID(body["public_id"])
                )
            )
        ).scalar_one()
        assert row.verdict == "permissible"
        assert isinstance(row.sources, list) and row.sources[0]["url"].startswith("https://")
        assert row.impure_income_pct == Decimal("10.0000")
        assert row.as_of_date == date(2026, 11, 1)


@pytest.mark.asyncio
async def test_agreeing_sources_not_flagged(client: AsyncClient):
    _, holding_id = await _holding(client, "cmp-agree")
    body = await _set_compliance(
        client,
        holding_id,
        sources=[_source(verdict="halal"), _source(screener_name="Musaffa", verdict="halal")],
        verdict="halal",
    )
    assert body["sources_disagree"] is False


@pytest.mark.asyncio
async def test_set_is_idempotent_upsert_and_audited(client: AsyncClient):
    _, holding_id = await _holding(client, "cmp-upsert")
    first = await _set_compliance(client, holding_id)
    again = await _set_compliance(client, holding_id)
    assert again["public_id"] == first["public_id"]
    assert again["created_at"] == first["created_at"]

    changed = await _set_compliance(
        client, holding_id, verdict="non_halal", impure_income_pct=None, as_of_date="2026-11-05"
    )
    assert changed["public_id"] == first["public_id"]
    assert changed["verdict"] == "non_halal"
    assert changed["impure_income_pct"] is None
    assert changed["updated_at"] >= first["updated_at"]

    async with postgres.async_session_maker() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(HoldingCompliance)
                .where(HoldingCompliance.public_id == UUID(first["public_id"]))
            )
        ).scalar_one()
        assert count == 1
        logs = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_type == "holding_compliance")
                    .order_by(AuditLog.id)
                )
            )
            .scalars()
            .all()
        )
        actions = [log.action for log in logs]
        assert actions == ["create", "update", "update"]
        last = logs[-1].details
        assert last["before"]["verdict"] == "permissible"
        assert last["after"]["verdict"] == "non_halal"
        assert "verdict" in last["changed_fields"]


@pytest.mark.asyncio
async def test_get_404_when_none(client: AsyncClient):
    _, holding_id = await _holding(client, "cmp-missing")
    assert (await client.get(_COMPLIANCE_URL.format(holding_id))).status_code == 404
    assert (await client.get(_COMPLIANCE_URL.format(uuid4()))).status_code == 404


@pytest.mark.asyncio
async def test_compliance_removed_with_holding(client: AsyncClient):
    _, holding_id = await _holding(client, "cmp-cascade")
    body = await _set_compliance(client, holding_id)
    assert (await client.delete(f"/v1/investing/holdings/{holding_id}")).status_code == 204
    async with postgres.async_session_maker() as session:
        gone = (
            await session.execute(
                select(HoldingCompliance).where(
                    HoldingCompliance.public_id == UUID(body["public_id"])
                )
            )
        ).scalar_one_or_none()
        assert gone is None


# ---------------------------------------------------------------------------
# Workspace isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_workspace_isolation(client: AsyncClient):
    account_map, holding_id = await _holding(client, "cmp-iso-a", "AAPL")
    await _set_compliance(client, holding_id, verdict="halal")
    dividend_id = await _create_dividend(client, account_map["brokerage"], "AAPL", "100.00")

    await client.post("/v1/auth/logout")
    await _register_and_login(
        client, email="cmp-iso-b@example.com", username="cmp-iso-b", password="TestPass123!"
    )
    # 404 (not 403) so the existence of A's holding/dividend is not leaked.
    assert (await client.get(_COMPLIANCE_URL.format(holding_id))).status_code == 404
    put = await client.put(_COMPLIANCE_URL.format(holding_id), json=_payload())
    assert put.status_code == 404
    assert (await client.get(_ESTIMATE_URL, params={"holding_id": holding_id})).status_code == 404
    assert (await client.get(_ESTIMATE_URL, params={"dividend_id": dividend_id})).status_code == 404

    # A's record is untouched by B's rejected write.
    async with postgres.async_session_maker() as session:
        verdicts = (
            (
                await session.execute(
                    select(HoldingCompliance.verdict)
                    .join(Holding, Holding.id == HoldingCompliance.holding_id)
                    .where(Holding.public_id == UUID(holding_id))
                )
            )
            .scalars()
            .all()
        )
        assert verdicts == ["halal"]


# ---------------------------------------------------------------------------
# Validation (422)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"impure_income_pct": "100.0001"},
        {"impure_income_pct": "-0.0001"},
        {"impure_income_pct": "1.123456"},
        {"verdict": "maybe"},
        {"sources": []},
        {"sources": [_source() for _ in range(11)]},
        {"sources": [_source(url="javascript:alert(1)")]},
        {"sources": [_source(url="ftp://example.com/x")]},
        {"sources": [_source(url="https://exa mple.com/x")]},
        {"sources": [_source(url="https://" + "a" * 2050 + ".com")]},
        {"sources": [_source(url="not a url")]},
        {"sources": [_source(screener_name="   ")]},
        {"sources": [_source(rule_reference="")]},
        {"sources": [_source(screener_name="x" * 101)]},
        {"sources": [_source(verdict="bogus")]},
        {"sources": [_source(as_of_date=(date.today() + timedelta(days=30)).isoformat())]},
        {"as_of_date": (datetime.now(UTC).date() + timedelta(days=30)).isoformat()},
        {"as_of_date": "not-a-date"},
    ],
)
async def test_compliance_rejects_invalid_input(client: AsyncClient, overrides: dict):
    _, holding_id = await _holding(client, "cmp-val")
    res = await client.put(_COMPLIANCE_URL.format(holding_id), json=_payload(**overrides))
    assert res.status_code == 422, res.text
    # Nothing was stored by a rejected write.
    assert (await client.get(_COMPLIANCE_URL.format(holding_id))).status_code == 404


@pytest.mark.asyncio
async def test_compliance_accepts_boundaries(client: AsyncClient):
    _, holding_id = await _holding(client, "cmp-bounds")
    for pct in ("0", "100", "100.0000"):
        body = await _set_compliance(client, holding_id, impure_income_pct=pct)
        assert Decimal(body["impure_income_pct"]) in (Decimal(0), Decimal(100))
    body = await _set_compliance(client, holding_id, sources=[_source() for _ in range(10)])
    assert len(body["sources"]) == 10


@pytest.mark.asyncio
async def test_db_constraints_reject_bad_rows(client: AsyncClient):
    account_map, holding_id = await _holding(client, "cmp-db")
    other_id = await _create_holding_via_order(
        client, account_map["brokerage"], "MSFT", "1.00000000", "100.00", "USD"
    )
    await _set_compliance(client, holding_id)

    async def _insert_fails(target_public_id: str, **kwargs) -> None:
        async with postgres.async_session_maker() as session:
            holding = (
                await session.execute(
                    select(Holding).where(Holding.public_id == UUID(target_public_id))
                )
            ).scalar_one()
            data = {"verdict": "halal", **kwargs}
            session.add(
                HoldingCompliance(
                    workspace_id=holding.workspace_id,
                    holding_id=holding.id,
                    sources=[],
                    as_of_date=date(2026, 11, 1),
                    **data,
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

    await _insert_fails(holding_id)  # unique (workspace_id, holding_id)
    await _insert_fails(other_id, impure_income_pct=Decimal("100.5"))  # pct range check
    await _insert_fails(other_id, impure_income_pct=Decimal("-1"))
    await _insert_fails(other_id, verdict="bogus")  # verdict check


# ---------------------------------------------------------------------------
# Purification estimates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purification_estimate_dividend(client: AsyncClient):
    account_map, holding_id = await _holding(client, "pur-div")
    await _set_compliance(client, holding_id, impure_income_pct="10.0000")
    dividend_id = await _create_dividend(
        client, account_map["brokerage"], "TLTM", "100.00", tax="15.00"
    )

    res = await client.get(_ESTIMATE_URL, params={"dividend_id": dividend_id})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["scope"] == "dividend"
    assert body["target_public_id"] == dividend_id
    assert body["holding_public_id"] == holding_id
    assert body["verdict"] == "permissible"
    assert body["compliance_on_file"] is True
    assert body["verdict_as_of_date"] == _AS_OF
    assert body["impure_income_pct"] == "10.0000"
    # Gross (100.00), not net (85.00): 10% -> 10.00
    assert body["base_amount"] == "100.00"
    assert body["purification_amount"] == "10.00"
    assert body["currency"] == "USD"
    assert body["dividend_count"] == 1
    assert "10.0000% non-permissible income" in body["basis"]
    assert "fatwa" in body["disclaimer"].lower()


@pytest.mark.asyncio
async def test_purification_estimate_holding_sums_dividends(client: AsyncClient):
    account_map, holding_id = await _holding(client, "pur-hold")
    await _set_compliance(client, holding_id, impure_income_pct="2.5")
    await _create_dividend(client, account_map["brokerage"], "TLTM", "100.00")
    await _create_dividend(
        client, account_map["brokerage"], "TLTM", "200.55", pay_date="2026-10-20"
    )
    await _create_dividend(client, account_map["brokerage"], "OTHER", "999.00")  # no holding

    res = await client.get(_ESTIMATE_URL, params={"holding_id": holding_id})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["scope"] == "holding"
    assert body["target_public_id"] == holding_id
    assert body["base_amount"] == "300.55"
    assert body["purification_amount"] == "7.51"  # 300.55 x 2.5% = 7.51375 -> half-up
    assert body["dividend_count"] == 2
    assert body["currency"] == "USD"

    windowed = (
        await client.get(
            _ESTIMATE_URL,
            params={"holding_id": holding_id, "from_date": "2026-10-10", "to_date": "2026-10-31"},
        )
    ).json()
    assert windowed["base_amount"] == "200.55"
    assert windowed["dividend_count"] == 1
    assert windowed["purification_amount"] == "5.01"  # 5.01375


@pytest.mark.asyncio
async def test_purification_estimate_zero_when_no_pct(client: AsyncClient):
    account_map, holding_id = await _holding(client, "pur-zero")
    await _set_compliance(client, holding_id, verdict="halal", impure_income_pct=None)
    dividend_id = await _create_dividend(client, account_map["brokerage"], "TLTM", "100.00")

    for params in ({"dividend_id": dividend_id}, {"holding_id": holding_id}):
        body = (await client.get(_ESTIMATE_URL, params=params)).json()
        assert body["purification_amount"] == "0.00"
        assert body["verdict"] == "halal"
        assert body["compliance_on_file"] is True
        assert body["impure_income_pct"] is None
        assert "no non-permissible-income percentage" in body["basis"]


@pytest.mark.asyncio
async def test_purification_estimate_zero_without_record(client: AsyncClient):
    account_map, holding_id = await _holding(client, "pur-norec")
    dividend_id = await _create_dividend(client, account_map["brokerage"], "TLTM", "100.00")
    for params in ({"dividend_id": dividend_id}, {"holding_id": holding_id}):
        body = (await client.get(_ESTIMATE_URL, params=params)).json()
        assert body["purification_amount"] == "0.00"
        assert body["verdict"] == "unknown"
        assert body["compliance_on_file"] is False
        assert "no compliance record" in body["basis"]


@pytest.mark.asyncio
async def test_purification_estimate_unlinked_dividend(client: AsyncClient):
    account_map, _ = await _holding(client, "pur-unlinked")
    dividend_id = await _create_dividend(client, account_map["brokerage"], "GONE", "50.00")
    body = (await client.get(_ESTIMATE_URL, params={"dividend_id": dividend_id})).json()
    assert body["purification_amount"] == "0.00"
    assert body["holding_public_id"] is None
    assert "not linked to a current holding" in body["basis"]


@pytest.mark.asyncio
async def test_purification_estimate_non_halal_notes_divestment(client: AsyncClient):
    account_map, holding_id = await _holding(client, "pur-nonhalal")
    await _set_compliance(client, holding_id, verdict="non_halal", impure_income_pct="40")
    await _create_dividend(client, account_map["brokerage"], "TLTM", "100.00")
    body = (await client.get(_ESTIMATE_URL, params={"holding_id": holding_id})).json()
    assert body["purification_amount"] == "40.00"
    assert any("divestment" in a for a in body["assumptions"])


@pytest.mark.asyncio
async def test_purification_estimate_param_validation(client: AsyncClient):
    _, holding_id = await _holding(client, "pur-val")
    await _set_compliance(client, holding_id)
    assert (await client.get(_ESTIMATE_URL)).status_code == 422
    both = await client.get(
        _ESTIMATE_URL, params={"holding_id": holding_id, "dividend_id": str(uuid4())}
    )
    assert both.status_code == 422
    assert (
        await client.get(_ESTIMATE_URL, params={"dividend_id": str(uuid4())})
    ).status_code == 404
    assert (await client.get(_ESTIMATE_URL, params={"holding_id": str(uuid4())})).status_code == 404
    assert (await client.get(_ESTIMATE_URL, params={"holding_id": "nope"})).status_code == 422
    reversed_range = await client.get(
        _ESTIMATE_URL,
        params={"holding_id": holding_id, "from_date": "2026-12-01", "to_date": "2026-01-01"},
    )
    assert reversed_range.status_code == 422
    window_on_dividend = await client.get(
        _ESTIMATE_URL, params={"dividend_id": str(uuid4()), "from_date": "2026-01-01"}
    )
    assert window_on_dividend.status_code == 422


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


async def _tool_map() -> dict:
    return {t.name: t.fn for t in await create_mcp_server().list_tools()}


@pytest.mark.asyncio
async def test_mcp_compliance_tools_end_to_end(client: AsyncClient, monkeypatch, mcp_enabled):
    account_map, holding_id = await _holding(client, "mcp-cmp")
    await _create_dividend(client, account_map["brokerage"], "TLTM", "200.00")
    async with postgres.async_session_maker() as session:
        holding = (
            await session.execute(select(Holding).where(Holding.public_id == UUID(holding_id)))
        ).scalar_one()
        workspace_id, user_id = holding.workspace_id, holding.user_id

    auth = AsyncMock(return_value=user_id)
    monkeypatch.setattr("app.mcp.security.authorize_workspace", auth)
    tools = await _tool_map()
    sources = [_source(verdict="halal"), _source(screener_name="S&P Shariah", verdict="non_halal")]

    # Not confirmed -> nothing is written and no authorization of a write happens.
    pending = await tools["set_holding_compliance"](
        workspace_id=workspace_id,
        holding_id=holding_id,
        verdict="permissible",
        sources=sources,
        as_of_date=_AS_OF,
        impure_income_pct="5",
    )
    assert pending["status"] == "needs_confirmation"
    empty = await tools["get_holding_compliance"](workspace_id=workspace_id, holding_id=holding_id)
    assert empty["status"] == "success" and empty["compliance"] is None

    saved = await tools["set_holding_compliance"](
        workspace_id=workspace_id,
        holding_id=holding_id,
        verdict="permissible",
        sources=sources,
        as_of_date=_AS_OF,
        impure_income_pct="5",
        confirmed=True,
    )
    assert saved["status"] == "success", saved
    assert saved["compliance"]["sources_disagree"] is True
    scopes = {call.kwargs["required_scope"] for call in auth.await_args_list}
    assert scopes == {"mcp:read", "mcp:research"}

    got = await tools["get_holding_compliance"](workspace_id=workspace_id, holding_id=holding_id)
    assert got["compliance"]["verdict"] == "permissible"
    assert got["compliance"]["sources"][1]["screener_name"] == "S&P Shariah"

    est = await tools["get_purification_estimate"](workspace_id=workspace_id, holding_id=holding_id)
    assert est["status"] == "success"
    assert est["estimate"]["purification_amount"] == "10.00"
    assert est["estimate"]["currency"] == "USD"

    # Same data over REST is identical (one code path).
    rest = (await client.get(_COMPLIANCE_URL.format(holding_id))).json()
    assert rest["sources"] == got["compliance"]["sources"]


@pytest.mark.asyncio
async def test_mcp_compliance_tools_errors(client: AsyncClient, monkeypatch, mcp_enabled):
    _, holding_id = await _holding(client, "mcp-cmp-err")
    async with postgres.async_session_maker() as session:
        holding = (
            await session.execute(select(Holding).where(Holding.public_id == UUID(holding_id)))
        ).scalar_one()
        workspace_id, user_id = holding.workspace_id, holding.user_id
    monkeypatch.setattr("app.mcp.security.authorize_workspace", AsyncMock(return_value=user_id))
    tools = await _tool_map()

    bad = await tools["set_holding_compliance"](
        workspace_id=workspace_id,
        holding_id=holding_id,
        verdict="halal",
        sources=[_source(url="javascript:alert(1)")],
        as_of_date=_AS_OF,
        confirmed=True,
    )
    assert bad["status"] == "error"
    unknown = await tools["set_holding_compliance"](
        workspace_id=workspace_id,
        holding_id=str(uuid4()),
        verdict="halal",
        sources=[_source()],
        as_of_date=_AS_OF,
        confirmed=True,
    )
    assert unknown["status"] == "error" and "not found" in unknown["message"].lower()
    for tool, kwargs in (
        ("get_holding_compliance", {"holding_id": "nope"}),
        ("get_holding_compliance", {"holding_id": str(uuid4())}),
        ("get_purification_estimate", {}),
        ("get_purification_estimate", {"holding_id": "nope"}),
        ("get_purification_estimate", {"holding_id": holding_id, "dividend_id": str(uuid4())}),
        ("get_purification_estimate", {"holding_id": holding_id, "from_date": "13-13-2026"}),
    ):
        res = await tools[tool](workspace_id=workspace_id, **kwargs)
        assert res["status"] == "error", (tool, kwargs, res)


@pytest.mark.asyncio
async def test_mcp_tool_docstrings_are_discoverable(mcp_enabled):
    tools = {t.name: t for t in await create_mcp_server().list_tools()}
    for name in ("set_holding_compliance", "get_holding_compliance", "get_purification_estimate"):
        assert tools[name].description and len(tools[name].description) > 80
    assert "fatwa" in tools["get_purification_estimate"].description
    assert "mcp:research" in tools["set_holding_compliance"].description


@pytest.mark.asyncio
async def test_list_holdings_includes_halal_verdict_and_theme_tags(
    client: AsyncClient, monkeypatch, mcp_enabled
):
    account_map = await _register_and_login(
        client,
        email="enrich-list@example.com",
        username="enrich-list",
        password="Abcdef1!",
    )
    brokerage = account_map["brokerage"]
    aapl_public_id = await _create_holding_via_order(
        client, brokerage, "AAPL", "10.00000000", "100.00", "USD"
    )
    msft_public_id = await _create_holding_via_order(
        client, brokerage, "MSFT", "10.00000000", "100.00", "USD"
    )

    async with postgres.async_session_maker() as session:
        aapl = (
            await session.execute(
                select(Holding).where(Holding.public_id == UUID(aapl_public_id))
            )
        ).scalar_one()
        msft = (
            await session.execute(
                select(Holding).where(Holding.public_id == UUID(msft_public_id))
            )
        ).scalar_one()
        workspace_id, user_id = aapl.workspace_id, aapl.user_id
        assert msft.workspace_id == workspace_id

    await _set_compliance(client, aapl_public_id, verdict="permissible", impure_income_pct="5")

    async with postgres.async_session_maker() as session:
        aapl = (
            await session.execute(
                select(Holding).where(Holding.public_id == UUID(aapl_public_id))
            )
        ).scalar_one()
        await HoldingThemeTagRepository(session).replace_for_holding(
            workspace_id, aapl.id, [("battery", None), ("AI power/grid", Decimal("10"))]
        )
        await session.commit()

    monkeypatch.setattr(
        "app.mcp.security.authorize_workspace", AsyncMock(return_value=user_id)
    )
    tools = await _tool_map()
    listing = await tools["list_investment_holdings"](workspace_id=workspace_id, limit=50)
    assert listing["status"] == "success", listing
    by_symbol = {h["symbol"]: h for h in listing["items"]}
    assert set(by_symbol) == {"AAPL", "MSFT"}

    aapl_item = by_symbol["AAPL"]
    assert aapl_item["halal_verdict"] == "permissible"
    assert aapl_item["theme_tags"] == ["AI power/grid", "battery"]

    msft_item = by_symbol["MSFT"]
    assert "halal_verdict" not in msft_item
    assert "theme_tags" not in msft_item
