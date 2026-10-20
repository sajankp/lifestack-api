"""End-to-end: creating the first account in a workspace seeds the default
custom KPIs, so the /spending/kpis tab and the dashboard "Custom KPIs" card
have something to show without the user creating anything by hand.
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_creating_first_account_seeds_default_kpis(client: AsyncClient) -> None:
    await client.post(
        "/v1/auth/register",
        json={
            "email": "kpi-seed@example.com",
            "username": "kpi_seed",
            "password": "TestPass123!",
        },
    )
    await client.post(
        "/v1/auth/login",
        data={"username": "kpi_seed", "password": "TestPass123!"},
    )

    res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Seed Bank", "account_type": "bank", "default_currency_code": "USD"},
    )
    assert res.status_code == 201, res.text

    kpi_res = await client.get("/v1/spending/kpis")
    assert kpi_res.status_code == 200, kpi_res.text
    body = kpi_res.json()
    assert body["total"] == 3, body
    metrics = {k["metric_type"] for k in body["items"]}
    assert metrics == {"spend_total", "income_total", "net_cash_flow"}
    # No targets: a seeded target would fire a breach todo on day one.
    assert all(k["target_value"] is None for k in body["items"])


@pytest.mark.asyncio
async def test_creating_a_second_account_does_not_reseed(client: AsyncClient) -> None:
    await client.post(
        "/v1/auth/register",
        json={
            "email": "kpi-seed2@example.com",
            "username": "kpi_seed2",
            "password": "TestPass123!",
        },
    )
    await client.post(
        "/v1/auth/login",
        data={"username": "kpi_seed2", "password": "TestPass123!"},
    )

    for name in ("First Bank", "Second Bank"):
        res = await client.post(
            "/v1/finance/accounts",
            json={"name": name, "account_type": "bank", "default_currency_code": "USD"},
        )
        assert res.status_code == 201, res.text

    kpi_res = await client.get("/v1/spending/kpis")
    assert kpi_res.status_code == 200, kpi_res.text
    assert kpi_res.json()["total"] == 3, kpi_res.json()


@pytest.mark.asyncio
async def test_no_account_means_no_kpis(client: AsyncClient) -> None:
    """A KPI cannot be evaluated over a workspace with no accounts."""
    await client.post(
        "/v1/auth/register",
        json={
            "email": "kpi-seed3@example.com",
            "username": "kpi_seed3",
            "password": "TestPass123!",
        },
    )
    await client.post(
        "/v1/auth/login",
        data={"username": "kpi_seed3", "password": "TestPass123!"},
    )

    kpi_res = await client.get("/v1/spending/kpis")
    assert kpi_res.status_code == 200, kpi_res.text
    assert kpi_res.json()["total"] == 0
