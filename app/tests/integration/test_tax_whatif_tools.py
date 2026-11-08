"""Integration tests for the read-only tax what-if MCP tools (spec-106).

Data is built through the real REST API (so lots come from the real FIFO
replay); the MCP tool functions are then called with ``authorize_workspace``
patched (the authorization itself is covered in test_mcp_security.py) so the
workspace id handed to a tool is exactly what the isolation tests vary.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import event, func, select

from app.auth.models import User
from app.config import settings
from app.core.database import postgres
from app.finance.models import Account, FxRate
from app.investing.models import (
    Holding,
    HoldingPrice,
    InvestingOrder,
    LotConsumption,
    OrderLot,
)
from app.investing.tax.fy import add_months
from app.mcp.server import create_mcp_server
from app.platform.models import WorkspaceMembership

NOW = datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Helpers (REST for data, DB for ids)
# --------------------------------------------------------------------------- #


async def _register(client: AsyncClient, suffix: str) -> dict:
    username = f"whatif-{suffix}"
    res = await client.post(
        "/v1/auth/register",
        json={"email": f"{username}@example.com", "username": username, "password": "TestPass123!"},
    )
    assert res.status_code == 200
    res = await client.post(
        "/v1/auth/login", data={"username": username, "password": "TestPass123!"}
    )
    assert res.status_code == 200
    async with postgres.async_session_maker() as session:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one()
        membership = (
            await session.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)
            )
        ).scalar_one()
        return {"user_id": user.id, "workspace_id": membership.workspace_id}


async def _account(
    client: AsyncClient, name: str, currency: str, market_scope: str | None = None
) -> str:
    res = await client.post(
        "/v1/finance/accounts",
        json={"name": name, "account_type": "brokerage", "default_currency_code": currency},
    )
    assert res.status_code == 201, res.text
    account_id = res.json()["public_id"]
    if market_scope:
        patch = await client.patch(
            f"/v1/finance/accounts/{account_id}", json={"market_scope": market_scope}
        )
        assert patch.status_code == 200, patch.text
    res = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_id,
            "balance": "10000000",
            "currency": currency,
            "as_of": (NOW - timedelta(days=2000)).isoformat(),
        },
    )
    assert res.status_code == 201, res.text
    return account_id


async def _order(
    client: AsyncClient,
    account_id: str,
    order_type: str,
    symbol: str,
    qty: str,
    price: str,
    when: datetime,
    currency: str,
) -> None:
    res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": account_id,
            "order_type": order_type,
            "symbol": symbol,
            "quantity": qty,
            "price_per_unit": price,
            "currency": currency,
            "occurred_at": when.isoformat(),
            "brokerage_fee": "0",
        },
    )
    assert res.status_code == 201, res.text


async def _seed_fx(currency: str = "USD", rate: str = "95.00") -> None:
    async with postgres.async_session_maker() as session:
        session.add(
            FxRate(
                base_currency_code=currency,
                quote_currency_code="INR",
                rate=Decimal(rate),
                as_of=NOW - timedelta(days=60),
                fetched_at=NOW,
                source="test",
            )
        )
        await session.commit()


async def _set_price(workspace_id: int, symbol: str, price: str, days_old: int = 0) -> None:
    async with postgres.async_session_maker() as session:
        holding = (
            await session.execute(
                select(Holding).where(
                    Holding.workspace_id == workspace_id, Holding.symbol == symbol
                )
            )
        ).scalar_one()
        session.add(
            HoldingPrice(
                workspace_id=workspace_id,
                holding_id=holding.id,
                price_date=(NOW - timedelta(days=days_old)).date(),
                unit_price=Decimal(price),
                source="manual",
            )
        )
        await session.commit()


async def _holding_ids(workspace_id: int) -> dict[tuple[str, str], str]:
    """(symbol, account name) -> holding public id."""
    async with postgres.async_session_maker() as session:
        rows = (
            await session.execute(
                select(Holding.symbol, Account.name, Holding.public_id)
                .join(Account, Account.id == Holding.account_id)
                .where(Holding.workspace_id == workspace_id)
            )
        ).all()
    return {(sym, name): str(pid) for sym, name, pid in rows}


async def _counts() -> dict[str, int]:
    out = {}
    async with postgres.async_session_maker() as session:
        for model in (Holding, OrderLot, LotConsumption, InvestingOrder, HoldingPrice):
            out[model.__name__] = (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
    return out


@pytest.fixture
async def tools(monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.lifestack.test")
    server = create_mcp_server()
    registered = {t.name: t.fn for t in await server.list_tools()}
    return registered


def _as(monkeypatch, user_id: int) -> None:
    monkeypatch.setattr("app.mcp.server.authorize_workspace", AsyncMock(return_value=user_id))


def _local_today() -> date:
    return datetime.now(UTC).astimezone(ZoneInfo("Asia/Kolkata")).date()


def _fy_start_utc(now: datetime) -> datetime:
    year = now.year if now.month >= 4 else now.year - 1
    return datetime(year, 4, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_foreign_lots_countdown_fx_source_and_incremental_tax(
    client: AsyncClient, tools, monkeypatch
):
    ctx = await _register(client, "foreign")
    _as(monkeypatch, ctx["user_id"])
    # High income so the engine's 87A rebate does not zero the small tax.
    patch = await client.patch("/v1/investing/tax/profile", json={"other_income": "3000000"})
    assert patch.status_code == 200, patch.text
    account = await _account(client, "us-broker", "USD")
    await _seed_fx("USD", "95.00")
    old = NOW - timedelta(days=800)  # > 24 months: long-term
    recent = NOW - timedelta(days=100)  # short-term
    await _order(client, account, "buy", "AAPL", "10", "100", old, "USD")
    await _order(client, account, "buy", "AAPL", "5", "150", recent, "USD")
    ids = await _holding_ids(ctx["workspace_id"])
    holding_id = ids[("AAPL", "us-broker")]
    before = await _counts()

    lots = await tools["list_tax_lots"](workspace_id=ctx["workspace_id"], holding=holding_id)
    assert lots["status"] == "success"
    assert lots["holding"]["regime"]["type"] == "foreign"
    assert lots["holding"]["regime"]["long_term_after_months"] == 24
    assert [lot["fifo_position"] for lot in lots["lots"]] == [1, 2]
    first, second = lots["lots"]
    assert first["quantity"] == "10.00000000" or Decimal(first["quantity"]) == 10
    assert first["is_long_term"] is True and first["days_to_long_term"] == 0
    assert second["is_long_term"] is False
    acquired = date.fromisoformat(second["acquired_date"])
    mark = add_months(datetime(acquired.year, acquired.month, acquired.day, tzinfo=UTC), 24)
    expected = (mark.date() + timedelta(days=1) - _local_today()).days
    assert second["days_to_long_term"] == expected > 0
    assert second["long_term_from"] == (mark.date() + timedelta(days=1)).isoformat()
    assert Decimal(second["cost_per_unit"]) == Decimal("150")

    sim = await tools["simulate_sale"](
        workspace_id=ctx["workspace_id"],
        holding=holding_id,
        qty="12",
        date=_local_today().isoformat(),
        price="200",
    )
    assert sim["status"] == "success", sim
    assert sim["fx"]["source"] == "app_rate_estimate"
    assert sim["fx"]["rate_to_inr"] == "95.0000000000"
    assert sim["fx"]["currency"] == "USD"
    consumed = sim["lots"]
    assert [Decimal(lot["quantity"]) for lot in consumed] == [Decimal("10"), Decimal("2")]
    assert consumed[0]["is_long_term"] is True and consumed[0]["days_to_long_term"] == 0
    assert consumed[1]["is_long_term"] is False and consumed[1]["days_to_long_term"] > 0
    assert Decimal(sim["gains"]["ltcg_native"]) == Decimal("1000.00")
    assert Decimal(sim["gains"]["stcg_native"]) == Decimal("100.00")
    assert Decimal(sim["gains"]["gain_inr"]) == Decimal("104500.00")
    assert Decimal(sim["tax"]["incremental_tax_inr"]) > 0
    assert any("Rule 115" in a for a in sim["assumptions"])
    assert any("incremental" in a for a in sim["assumptions"])
    # Read-only: no row of any investing table changed.
    assert await _counts() == before


@pytest.mark.asyncio
async def test_missing_fx_rate_path_returns_no_tax_figure(client: AsyncClient, tools, monkeypatch):
    ctx = await _register(client, "nofx")
    _as(monkeypatch, ctx["user_id"])
    account = await _account(client, "uk-broker", "GBP")  # no GBP->INR rate seeded
    await _order(client, account, "buy", "VOD", "10", "100", NOW - timedelta(days=900), "GBP")
    holding_id = (await _holding_ids(ctx["workspace_id"]))[("VOD", "uk-broker")]

    sim = await tools["simulate_sale"](
        workspace_id=ctx["workspace_id"],
        holding=holding_id,
        qty="10",
        date=_local_today().isoformat(),
        price="120",
    )
    assert sim["status"] == "missing_fx_rate"
    assert sim["tax"] is None
    assert sim["fx"] is None
    assert sim["gains"]["gain_inr"] is None
    assert Decimal(sim["gains"]["gain_native"]) == Decimal("200.00")
    assert sim["missing_fx_rates"][0]["currency"] == "GBP"
    assert "No Rule 115" in sim["message"]


@pytest.mark.asyncio
async def test_insufficient_quantity_simulates_the_maximum(client: AsyncClient, tools, monkeypatch):
    ctx = await _register(client, "insuff")
    _as(monkeypatch, ctx["user_id"])
    account = await _account(client, "us-broker", "USD")
    await _seed_fx()
    await _order(client, account, "buy", "AAPL", "10", "100", NOW - timedelta(days=800), "USD")
    holding_id = (await _holding_ids(ctx["workspace_id"]))[("AAPL", "us-broker")]

    sim = await tools["simulate_sale"](
        workspace_id=ctx["workspace_id"],
        holding=holding_id,
        qty="25",
        date=_local_today().isoformat(),
        price="150",
    )
    assert sim["status"] == "insufficient_quantity"
    assert Decimal(sim["sale"]["quantity_requested"]) == 25
    assert Decimal(sim["sale"]["quantity_consumed"]) == 10
    assert "only 10" in sim["message"]
    assert sim["tax"] is not None  # the maximum sellable quantity is still priced


@pytest.mark.asyncio
async def test_indian_exemption_accounts_for_gains_already_realised(
    client: AsyncClient, tools, monkeypatch
):
    ctx = await _register(client, "exempt")
    _as(monkeypatch, ctx["user_id"])
    patch = await client.patch(
        "/v1/investing/tax/profile", json={"other_income": "3000000", "tax_regime": "new"}
    )
    assert patch.status_code == 200, patch.text
    account = await _account(client, "in-broker", "INR", market_scope="india")
    realised_at = max(NOW - timedelta(days=10), _fy_start_utc(NOW))
    await _order(client, account, "buy", "INFY", "2000", "100", NOW - timedelta(days=800), "INR")
    # +₹1.2 lakh LTCG already booked this FY.
    await _order(client, account, "sell", "INFY", "1000", "220", realised_at, "INR")
    holding_id = (await _holding_ids(ctx["workspace_id"]))[("INFY", "in-broker")]
    summary = await tools["get_tax_summary"](workspace_id=ctx["workspace_id"])
    assert summary["status"] == "success"
    assert Decimal(summary["total_gain_inr"]) == Decimal("120000.00")

    before = await _counts()
    sim = await tools["simulate_sale"](
        workspace_id=ctx["workspace_id"],
        holding=holding_id,
        qty="400",
        date=realised_at.date().isoformat(),
        price="225",
    )
    assert sim["status"] == "success", sim
    assert sim["holding"]["regime"]["type"] == "indian"
    tax = sim["tax"]
    assert tax["realised_lots_in_fy"] == 1
    assert Decimal(tax["realised_ltcg_in_fy_inr"]) == Decimal("120000.00")
    assert Decimal(tax["ltcg_exemption_remaining_inr"]) == Decimal("5000.00")
    assert Decimal(tax["ltcg_exemption_used_by_sale_inr"]) == Decimal("5000.00")
    # Same figure get_tax_summary reports for the FY today.
    assert Decimal(tax["fy_tax_before_inr"]) == Decimal(summary["total_tax_inr"])
    # (₹50,000 gain - ₹5,000 left of the exemption) x 12.5% x 1.04
    assert Decimal(tax["incremental_tax_inr"]) == Decimal("5850.00")
    assert any("Sec 112A exemption" in a for a in sim["assumptions"])
    assert await _counts() == before


@pytest.mark.asyncio
async def test_symbol_ambiguity_is_rejected_and_resolvable(client: AsyncClient, tools, monkeypatch):
    ctx = await _register(client, "ambig")
    _as(monkeypatch, ctx["user_id"])
    acct_a = await _account(client, "broker-a", "USD")
    acct_b = await _account(client, "broker-b", "USD")
    await _seed_fx()
    when = NOW - timedelta(days=800)
    await _order(client, acct_a, "buy", "MSFT", "3", "100", when, "USD")
    await _order(client, acct_b, "buy", "MSFT", "4", "100", when, "USD")
    ids = await _holding_ids(ctx["workspace_id"])

    res = await tools["list_tax_lots"](workspace_id=ctx["workspace_id"], holding="msft")
    assert res["status"] == "error" and res["code"] == "ambiguous_holding"
    assert {c["account_name"] for c in res["candidates"]} == {"broker-a", "broker-b"}
    assert {c["holding_id"] for c in res["candidates"]} == {
        ids[("MSFT", "broker-a")],
        ids[("MSFT", "broker-b")],
    }

    ok = await tools["list_tax_lots"](
        workspace_id=ctx["workspace_id"], holding="MSFT", account_id=acct_b
    )
    assert ok["status"] == "success"
    assert Decimal(ok["lots"][0]["quantity"]) == 4
    by_id = await tools["list_tax_lots"](
        workspace_id=ctx["workspace_id"], holding=ids[("MSFT", "broker-a")]
    )
    assert Decimal(by_id["lots"][0]["quantity"]) == 3
    # A public id that conflicts with the account filter is not silently accepted.
    clash = await tools["list_tax_lots"](
        workspace_id=ctx["workspace_id"], holding=ids[("MSFT", "broker-a")], account_id=acct_b
    )
    assert clash["code"] == "holding_not_found"


@pytest.mark.asyncio
async def test_invalid_inputs_are_rejected_before_any_work(tools, monkeypatch):
    _as(monkeypatch, 1)
    base = {"workspace_id": 1, "holding": "AAPL", "qty": "1", "date": "2026-10-16", "price": "1"}
    for override, code in [
        ({"qty": "0"}, "invalid_number"),
        ({"qty": "abc"}, "invalid_number"),
        ({"qty": "NaN"}, "invalid_number"),
        ({"qty": "1.123456789"}, "invalid_number"),
        ({"price": "-1"}, "invalid_number"),
        ({"price": "1e30"}, "invalid_number"),
        ({"sell_fees": "-0.01"}, "invalid_number"),
    ]:
        res = await tools["simulate_sale"](**{**base, **override})
        assert res["status"] == "error" and res["code"] == code, override


@pytest.mark.asyncio
async def test_bad_date_unknown_holding_and_bad_ids(client: AsyncClient, tools, monkeypatch):
    ctx = await _register(client, "badin")
    _as(monkeypatch, ctx["user_id"])
    base = {"workspace_id": ctx["workspace_id"], "holding": "AAPL", "qty": "1", "price": "1"}
    for bad in ["", "tomorrow", "2026-13-40", "1999-01-01"]:
        res = await tools["simulate_sale"](**base, date=bad)
        assert res["code"] == "invalid_date", bad
    res = await tools["simulate_sale"](**base, date="2026-10-16")
    assert res["code"] == "holding_not_found"
    res = await tools["list_tax_lots"](
        workspace_id=ctx["workspace_id"], holding="AAPL", account_id="not-a-uuid"
    )
    assert res["code"] == "invalid_account"
    res = await tools["list_tax_lots"](
        workspace_id=ctx["workspace_id"],
        holding="AAPL",
        account_id="00000000-0000-0000-0000-000000000000",
    )
    assert res["code"] == "account_not_found"
    res = await tools["list_tax_lots"](workspace_id=ctx["workspace_id"], holding="AAPL", limit=0)
    assert res["code"] == "invalid_page"
    res = await tools["harvest_candidates"](workspace_id=ctx["workspace_id"], limit=201)
    assert res["code"] == "invalid_page"
    res = await tools["harvest_candidates"](
        workspace_id=ctx["workspace_id"], stcg_rate_percent="150"
    )
    assert res["code"] == "invalid_number"


@pytest.mark.asyncio
async def test_cross_workspace_isolation(client: AsyncClient, tools, monkeypatch):
    a = await _register(client, "iso-a")
    acct_a = await _account(client, "a-broker", "USD")
    await _seed_fx()
    await _order(client, acct_a, "buy", "TSLA", "7", "100", NOW - timedelta(days=800), "USD")
    await _set_price(a["workspace_id"], "TSLA", "50")
    a_holding = (await _holding_ids(a["workspace_id"]))[("TSLA", "a-broker")]

    b = await _register(client, "iso-b")
    acct_b = await _account(client, "b-broker", "USD")
    await _order(client, acct_b, "buy", "NVDA", "2", "100", NOW - timedelta(days=800), "USD")
    await _set_price(b["workspace_id"], "NVDA", "60")
    _as(monkeypatch, b["user_id"])

    # B asks for A's holding by public id (and by A's symbol): nothing leaks.
    for ref in (a_holding, "TSLA"):
        res = await tools["list_tax_lots"](workspace_id=b["workspace_id"], holding=ref)
        assert res["code"] == "holding_not_found"
        res = await tools["simulate_sale"](
            workspace_id=b["workspace_id"],
            holding=ref,
            qty="1",
            date=_local_today().isoformat(),
            price="1",
        )
        assert res["code"] == "holding_not_found"

    # Harvest in B sees only B's holding.
    harvest = await tools["harvest_candidates"](workspace_id=b["workspace_id"])
    assert {c["symbol"] for c in harvest["candidates"]} == {"NVDA"}
    assert all(s["symbol"] != "TSLA" for s in harvest["skipped_holdings"])

    # And A's own view still works.
    _as(monkeypatch, a["user_id"])
    res = await tools["list_tax_lots"](workspace_id=a["workspace_id"], holding=a_holding)
    assert res["status"] == "success" and Decimal(res["lots"][0]["quantity"]) == 7


@pytest.mark.asyncio
async def test_sold_out_lots_are_excluded_and_partial_lots_show_remaining(
    client: AsyncClient, tools, monkeypatch
):
    ctx = await _register(client, "remain")
    _as(monkeypatch, ctx["user_id"])
    account = await _account(client, "us-broker", "USD")
    await _seed_fx()
    await _order(client, account, "buy", "AAPL", "10", "100", NOW - timedelta(days=900), "USD")
    await _order(client, account, "buy", "AAPL", "10", "110", NOW - timedelta(days=700), "USD")
    await _order(client, account, "buy", "AAPL", "10", "120", NOW - timedelta(days=500), "USD")
    # Sells the whole first lot and 4 of the second.
    await _order(client, account, "sell", "AAPL", "14", "130", NOW - timedelta(days=5), "USD")
    holding_id = (await _holding_ids(ctx["workspace_id"]))[("AAPL", "us-broker")]
    res = await tools["list_tax_lots"](workspace_id=ctx["workspace_id"], holding=holding_id)
    assert [Decimal(lot["quantity"]) for lot in res["lots"]] == [Decimal("6"), Decimal("10")]
    assert [Decimal(lot["cost_per_unit"]) for lot in res["lots"]] == [
        Decimal("110"),
        Decimal("120"),
    ]
    assert Decimal(res["holding"]["open_lots_quantity"]) == Decimal(
        res["holding"]["holding_quantity"]
    )
    assert res["warnings"] == [] or all("No stored price" in w for w in res["warnings"])


@pytest.mark.asyncio
async def test_holding_without_lots_is_flagged_not_hidden(client: AsyncClient, tools, monkeypatch):
    ctx = await _register(client, "nolots")
    _as(monkeypatch, ctx["user_id"])
    account = await _account(client, "us-broker", "USD")
    # A holding that did not come from orders (e.g. a manual/imported
    # position) has no OrderLot rows.
    async with postgres.async_session_maker() as session:
        account_row = (
            await session.execute(select(Account).where(Account.public_id == UUID(account)))
        ).scalar_one()
        session.add(
            Holding(
                workspace_id=ctx["workspace_id"],
                user_id=ctx["user_id"],
                symbol="IMPORTED",
                account_id=account_row.id,
                quantity=Decimal("5"),
                avg_cost=Decimal("10"),
                currency="USD",
            )
        )
        await session.commit()
    holding_id = (await _holding_ids(ctx["workspace_id"]))[("IMPORTED", "us-broker")]
    listing = await tools["list_tax_lots"](workspace_id=ctx["workspace_id"], holding=holding_id)
    assert listing["lots"] == []
    assert any("open lots total 0" in w for w in listing["warnings"])


@pytest.mark.asyncio
async def test_harvest_ranks_flags_stale_and_missing_prices_and_fx(
    client: AsyncClient, tools, monkeypatch
):
    ctx = await _register(client, "harvest")
    _as(monkeypatch, ctx["user_id"])
    usd = await _account(client, "us-broker", "USD")
    inr = await _account(client, "in-broker", "INR", market_scope="india")
    gbp = await _account(client, "uk-broker", "GBP")
    await _seed_fx("USD", "95.00")  # no GBP rate on purpose
    old = NOW - timedelta(days=800)
    await _order(client, usd, "buy", "AAPL", "10", "100", old, "USD")
    await _order(client, usd, "buy", "AAPL", "5", "150", NOW - timedelta(days=100), "USD")
    await _order(client, inr, "buy", "INFY", "100", "1000", old, "INR")
    await _order(client, inr, "buy", "NOPRICE", "10", "100", old, "INR")
    await _order(client, gbp, "buy", "VOD", "10", "100", old, "GBP")
    ws = ctx["workspace_id"]
    await _set_price(ws, "AAPL", "80")  # loss: LT 200 USD, ST 350 USD
    await _set_price(ws, "INFY", "900", days_old=30)  # loss 10,000 INR, stale price
    await _set_price(ws, "VOD", "60")  # loss 400 GBP, no GBP->INR rate
    before = await _counts()

    res = await tools["harvest_candidates"](workspace_id=ws)
    assert res["status"] == "success"
    by_symbol = {}
    for c in res["candidates"]:
        by_symbol.setdefault(c["symbol"], []).append(c)

    # AAPL long-term lot: 200 USD x 12.5% = 25 USD = ₹2,375 at 95.
    aapl_lt = next(c for c in by_symbol["AAPL"] if c["is_long_term"])
    assert Decimal(aapl_lt["unrealised_loss_native"]) == Decimal("200.00")
    assert Decimal(aapl_lt["tax_saved_native"]) == Decimal("25.00")
    assert Decimal(aapl_lt["tax_saved_inr"]) == Decimal("2375.00")
    assert aapl_lt["price_is_stale"] is False
    # AAPL short-term lot is slab-dependent: unranked, no figure invented.
    aapl_st = next(c for c in by_symbol["AAPL"] if not c["is_long_term"])
    assert aapl_st["tax_saved_native"] is None and aapl_st["rate_percent"] is None
    # INFY: LT, 10,000 x 12.5% = 1,250 INR, stale price flagged.
    infy = by_symbol["INFY"][0]
    assert Decimal(infy["tax_saved_inr"]) == Decimal("1250.00")
    assert infy["price_is_stale"] is True
    assert infy["price_date"] == (NOW - timedelta(days=30)).date().isoformat()
    assert any("INFY" in w for w in res["warnings"])
    # VOD: no GBP rate -> native saving only, flagged.
    vod = by_symbol["VOD"][0]
    assert Decimal(vod["tax_saved_native"]) == Decimal("50.00")
    assert vod["tax_saved_inr"] is None
    assert res["missing_fx_currencies"] == ["GBP"]
    # Missing price is reported, never guessed.
    skipped = {s["symbol"]: s["reason"] for s in res["skipped_holdings"]}
    assert skipped == {"NOPRICE": "no_price"}
    # Ranking: INR-known savings first (largest first), then native-only, then unranked.
    order = [(c["symbol"], c["is_long_term"]) for c in res["candidates"]]
    assert order == [("AAPL", True), ("INFY", True), ("VOD", True), ("AAPL", False)]

    # A supplied STCG rate ranks the foreign short-term lot.
    ranked = await tools["harvest_candidates"](workspace_id=ws, stcg_rate_percent="30", limit=1)
    assert ranked["returned"] == 1 and ranked["candidate_count"] == 4
    assert ranked["candidates"][0]["symbol"] == "AAPL"
    assert await _counts() == before


@pytest.mark.asyncio
async def test_large_lot_count_uses_a_constant_number_of_queries(
    client: AsyncClient, tools, monkeypatch
):
    """60 lots in one holding must not add per-lot queries (no N+1)."""
    ctx = await _register(client, "bulk")
    _as(monkeypatch, ctx["user_id"])
    account = await _account(client, "us-broker", "USD")
    await _seed_fx()
    for i in range(60):
        await _order(
            client, account, "buy", "AAPL", "1", "100", NOW - timedelta(days=800 - i), "USD"
        )
    holding_id = (await _holding_ids(ctx["workspace_id"]))[("AAPL", "us-broker")]
    statements: list[str] = []

    def _count(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(postgres.engine.sync_engine, "before_cursor_execute", _count)
    try:
        res = await tools["list_tax_lots"](workspace_id=ctx["workspace_id"], holding=holding_id)
        listing_queries = len(statements)
        statements.clear()
        sim = await tools["simulate_sale"](
            workspace_id=ctx["workspace_id"],
            holding=holding_id,
            qty="60",
            date=_local_today().isoformat(),
            price="150",
        )
    finally:
        event.remove(postgres.engine.sync_engine, "before_cursor_execute", _count)
    assert res["lot_count"] == 60 and len(res["lots"]) == 60
    assert sim["status"] == "success" and len(sim["lots"]) == 60
    assert listing_queries <= 15
    assert len(statements) <= 25

    page = await tools["list_tax_lots"](
        workspace_id=ctx["workspace_id"], holding=holding_id, limit=25, offset=50
    )
    assert page["lot_count"] == 60 and len(page["lots"]) == 10 and page["has_more"] is False


@pytest.mark.asyncio
async def test_user_timezone_decides_the_as_of_day(client: AsyncClient, tools, monkeypatch):
    ctx = await _register(client, "tz")
    _as(monkeypatch, ctx["user_id"])
    account = await _account(client, "us-broker", "USD")
    await _order(client, account, "buy", "AAPL", "1", "100", NOW - timedelta(days=800), "USD")
    holding_id = (await _holding_ids(ctx["workspace_id"]))[("AAPL", "us-broker")]
    async with postgres.async_session_maker() as session:
        user = await session.get(User, ctx["user_id"])
        user.timezone = "Pacific/Kiritimati"  # UTC+14: a different calendar day from UTC
        await session.commit()
    res = await tools["list_tax_lots"](workspace_id=ctx["workspace_id"], holding=holding_id)
    assert res["timezone"] == "Pacific/Kiritimati"
    assert (
        res["as_of"]
        == datetime.now(UTC).astimezone(ZoneInfo("Pacific/Kiritimati")).date().isoformat()
    )
