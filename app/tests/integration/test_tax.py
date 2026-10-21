"""Integration tests for the capital gains tax endpoints (spec-099)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient

from app.core.database import postgres
from app.finance.models import FxRate


async def _register(client: AsyncClient, suffix: str) -> None:
    register_res = await client.post(
        "/v1/auth/register",
        json={
            "email": f"tax-{suffix}@example.com",
            "username": f"tax-{suffix}",
            "password": "TestPass123!",
        },
    )
    assert register_res.status_code == 200
    login_res = await client.post(
        "/v1/auth/login",
        data={"username": f"tax-{suffix}", "password": "TestPass123!"},
    )
    assert login_res.status_code == 200


async def _create_brokerage_account(
    client: AsyncClient, name: str, currency: str = "USD"
) -> str:
    res = await client.post(
        "/v1/finance/accounts",
        json={
            "name": name,
            "account_type": "brokerage",
            "default_currency_code": currency,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["public_id"]


async def _fund_account(client: AsyncClient, account_id: str, amount: str = "100000") -> None:
    """Orders are cash-settled, so the account needs a balance first."""
    res = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_id,
            "balance": amount,
            "currency": "USD",
            "as_of": datetime.now(UTC).isoformat(),
        },
    )
    assert res.status_code == 201, res.text


async def _seed_fx_rate(
    client: AsyncClient, *, currency: str = "USD", rate: str = "95.00"
) -> None:
    """Seed a USD->INR rate so the Rule 115 conversion has a rate to use.

    The app stores daily market rates; the tax engine takes the latest one
    on or before the specified date.
    """
    # The engine takes the latest rate on or before the Rule 115 specified
    # date (the last day of the month before the sale). Seed a rate well
    # before that so it is always in range for a sale in the current FY.
    now = datetime.now(UTC)
    as_of = datetime(now.year, now.month, 1, tzinfo=UTC) - timedelta(days=1)
    async with postgres.async_session_maker() as session:
        session.add(
            FxRate(
                base_currency_code=currency,
                quote_currency_code="INR",
                rate=Decimal(rate),
                as_of=as_of,
                fetched_at=now,
                source="test",
            )
        )
        await session.commit()


async def _place_order(
    client: AsyncClient,
    account_id: str,
    *,
    order_type: str,
    symbol: str,
    quantity: str,
    price: str,
    occurred_at: str,
    currency: str = "USD",
    fees: str = "0",
) -> dict:
    res = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": account_id,
            "order_type": order_type,
            "symbol": symbol,
            "quantity": quantity,
            "price_per_unit": price,
            "currency": currency,
            "occurred_at": occurred_at,
            "brokerage_fee": fees,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


@pytest.mark.asyncio
async def test_tax_summary_returns_empty_for_a_workspace_with_no_sells(client: AsyncClient):
    await _register(client, "empty")
    res = await client.get("/v1/investing/tax/summary")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["buckets"] == {}
    assert body["total_tax_inr"] == "0"


@pytest.mark.asyncio
async def test_tax_summary_rejects_a_malformed_fy(client: AsyncClient):
    await _register(client, "badfy")
    res = await client.get("/v1/investing/tax/summary", params={"fy": "not-a-year"})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_tax_summary_computes_a_long_term_gain(client: AsyncClient):
    await _register(client, "ltcg")
    account_id = await _create_brokerage_account(client, "us-brokerage")
    await _fund_account(client, account_id)
    await _seed_fx_rate(client)
    # Bought 2 years + 3 months ago, sold inside the current FY.
    bought = datetime.now(UTC) - timedelta(days=800)
    sold = datetime.now(UTC) - timedelta(days=10)
    await _place_order(
        client,
        account_id,
        order_type="buy",
        symbol="AAPL",
        quantity="10",
        price="100",
        occurred_at=bought.isoformat(),
    )
    await _place_order(
        client,
        account_id,
        order_type="sell",
        symbol="AAPL",
        quantity="10",
        price="200",
        occurred_at=sold.isoformat(),
    )
    res = await client.get("/v1/investing/tax/summary")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    # The reconciliation guard passed: the FIFO replay reproduced the
    # recorded gain.
    assert body["reconciliation_failures"] == []
    assert body["positions"], "expected at least one consumed lot"
    assert body["positions"][0]["is_long_term"] is True
    assert Decimal(body["total_gain_inr"]) > 0


@pytest.mark.asyncio
async def test_tax_regimes_endpoint_lists_both_regimes(client: AsyncClient):
    await _register(client, "regimes")
    res = await client.get("/v1/investing/tax/regimes")
    assert res.status_code == 200
    regimes = {r["key"]: r for r in res.json()}
    assert set(regimes) == {"indian", "foreign"}
    # The foreign regime is the one that is easy to get wrong.
    assert regimes["foreign"]["ltcg_holding_months"] == 24
    assert regimes["foreign"]["ltcg_exemption_inr"] == "0"
    assert regimes["foreign"]["stcg_is_flat"] is False
    assert regimes["indian"]["ltcg_holding_months"] == 12
    assert regimes["indian"]["ltcg_exemption_inr"] == "125000"
    assert regimes["indian"]["stcg_is_flat"] is True


@pytest.mark.asyncio
async def test_tax_profile_get_and_patch(client: AsyncClient):
    await _register(client, "profile")
    res = await client.get("/v1/investing/tax/profile")
    assert res.status_code == 200
    body = res.json()
    assert body["tax_regime"] == "new"
    assert body["other_income"] == "0"

    patch = await client.patch(
        "/v1/investing/tax/profile",
        json={
            "other_income": "1500000",
            "tax_regime": "old",
            "fx_overrides": {"2026-10": "95.42"},
        },
    )
    assert patch.status_code == 200
    updated = patch.json()
    assert updated["other_income"] == "1500000"
    assert updated["tax_regime"] == "old"
    assert updated["fx_overrides"]["2026-10"] == "95.42"


@pytest.mark.asyncio
async def test_tax_profile_rejects_an_invalid_tax_regime(client: AsyncClient):
    await _register(client, "badregime")
    res = await client.patch(
        "/v1/investing/tax/profile", json={"tax_regime": "something-else"}
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_tax_profile_rejects_a_negative_other_income(client: AsyncClient):
    await _register(client, "negincome")
    res = await client.patch(
        "/v1/investing/tax/profile", json={"other_income": "-1"}
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_account_market_scope_is_settable_on_a_brokerage_account(client: AsyncClient):
    await _register(client, "scope")
    account_id = await _create_brokerage_account(client, "us-brokerage")
    res = await client.patch(
        f"/v1/finance/accounts/{account_id}", json={"market_scope": "foreign"}
    )
    assert res.status_code == 200
    assert res.json()["market_scope"] == "foreign"


@pytest.mark.asyncio
async def test_account_market_scope_is_rejected_on_a_non_brokerage_account(client: AsyncClient):
    await _register(client, "wattlescope")
    res = await client.post(
        "/v1/finance/accounts",
        json={
            "name": "savings",
            "account_type": "wallet",
            "default_currency_code": "USD",
        },
    )
    assert res.status_code == 201
    account_id = res.json()["public_id"]
    patch = await client.patch(
        f"/v1/finance/accounts/{account_id}", json={"market_scope": "foreign"}
    )
    assert patch.status_code == 422


@pytest.mark.asyncio
async def test_account_market_scope_rejects_an_unknown_value(client: AsyncClient):
    await _register(client, "badscope")
    account_id = await _create_brokerage_account(client, "us-brokerage")
    res = await client.patch(
        f"/v1/finance/accounts/{account_id}", json={"market_scope": "mars"}
    )
    assert res.status_code == 422


async def _create_holding_instrument(client: AsyncClient, symbol: str) -> str:
    """Place a buy order so the instrument exists, then return its public_id."""
    account_id = await _create_brokerage_account(client, f"acct-{symbol.lower()}")
    await _fund_account(client, account_id)
    await _place_order(
        client,
        account_id,
        order_type="buy",
        symbol=symbol,
        quantity="1",
        price="100",
        occurred_at=datetime.now(UTC).isoformat(),
    )
    res = await client.get("/v1/investing/instruments")
    assert res.status_code == 200
    body = res.json()
    items = body["items"] if isinstance(body, dict) else body
    instrument = next(i for i in items if i["symbol"] == symbol.upper())
    return instrument["public_id"]


@pytest.mark.asyncio
async def test_instrument_exchange_country_override_is_settable(client: AsyncClient):
    await _register(client, "override")
    instrument_id = await _create_holding_instrument(client, "AAPL")
    res = await client.patch(
        f"/v1/investing/instruments/{instrument_id}",
        json={"exchange_country_override": "IN"},
    )
    assert res.status_code == 200
    assert res.json()["exchange_country_override"] == "IN"


@pytest.mark.asyncio
async def test_instrument_exchange_country_override_rejects_a_bad_country(client: AsyncClient):
    await _register(client, "badcountry")
    instrument_id = await _create_holding_instrument(client, "AAPL")
    res = await client.patch(
        f"/v1/investing/instruments/{instrument_id}",
        json={"exchange_country_override": "XX"},
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_tax_summary_falls_back_to_the_currency_when_the_master_has_no_entry(
    client: AsyncClient,
):
    """A ticker absent from the security master falls through to the
    account scope, then the currency heuristic — never a guess."""
    await _register(client, "fallback")
    account_id = await _create_brokerage_account(client, "us-brokerage")
    await _fund_account(client, account_id)
    await _seed_fx_rate(client)
    bought = datetime.now(UTC) - timedelta(days=800)
    sold = datetime.now(UTC) - timedelta(days=10)
    await _place_order(
        client,
        account_id,
        order_type="buy",
        symbol="ZZZZ",
        quantity="10",
        price="100",
        occurred_at=bought.isoformat(),
    )
    await _place_order(
        client,
        account_id,
        order_type="sell",
        symbol="ZZZZ",
        quantity="10",
        price="200",
        occurred_at=sold.isoformat(),
    )
    res = await client.get("/v1/investing/tax/summary")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["positions"]
    # A USD holding with no master entry classifies as foreign.
    assert body["positions"][0]["regime"] == "foreign"
    assert body["positions"][0]["classified_by"] in {
        "account_market_scope",
        "currency_fallback",
    }

@pytest.mark.asyncio
async def test_tax_summary_reports_missing_fx_rates_instead_of_a_number(
    client: AsyncClient,
):
    """A foreign sale with no Rule 115 rate must not produce a tax number.

    Omitting the unconvertible gain would understate the tax, so the
    engine reports the gap instead.
    """
    await _register(client, "nofx")
    account_id = await _create_brokerage_account(client, "us-brokerage")
    await _fund_account(client, account_id)
    bought = datetime.now(UTC) - timedelta(days=800)
    sold = datetime.now(UTC) - timedelta(days=10)
    await _place_order(
        client,
        account_id,
        order_type="buy",
        symbol="NOFX",
        quantity="10",
        price="100",
        occurred_at=bought.isoformat(),
    )
    await _place_order(
        client,
        account_id,
        order_type="sell",
        symbol="NOFX",
        quantity="10",
        price="200",
        occurred_at=sold.isoformat(),
    )
    res = await client.get("/v1/investing/tax/summary")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "missing_fx_rate"
    assert body["total_tax_inr"] == "0"
    assert body["missing_fx_rates"]
    assert body["missing_fx_rates"][0]["currency"] == "USD"
    assert "Rule 115" in body["message"]


@pytest.mark.asyncio
async def test_instrument_override_on_a_global_reference_forks_a_workspace_copy(
    client: AsyncClient,
):
    """The security master is global; a per-holding override is a workspace
    fact, so the override lands on a workspace-scoped copy."""
    await _register(client, "fork")
    instrument_id = await _create_holding_instrument(client, "FORKED")
    before = await client.get("/v1/investing/instruments")
    before_items = before.json()["items"] if isinstance(before.json(), dict) else before.json()
    target = next(i for i in before_items if i["symbol"] == "FORKED")
    assert target["exchange_country_override"] is None

    res = await client.patch(
        f"/v1/investing/instruments/{instrument_id}",
        json={"exchange_country_override": "IN"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["exchange_country_override"] == "IN"
