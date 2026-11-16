from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings, settings
from app.main import app, create_app
from app.tests.integration.test_spending import _register_and_login


async def _e2e_hook_client():
    async def add_csrf_header(request):
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        if "x-csrf-token" in request.headers:
            return

        cookie_header = request.headers.get("cookie")
        if not cookie_header:
            return

        cookie = SimpleCookie()
        cookie.load(cookie_header)
        csrf_token = cookie.get("csrf_token")
        if csrf_token:
            request.headers["X-CSRF-Token"] = csrf_token.value

    return AsyncClient(
        transport=ASGITransport(app=create_app(), client=("127.0.0.1", 123)),
        base_url="http://test",
        headers={"Origin": "http://test"},
        event_hooks={"request": [add_csrf_header]},
    )


@pytest.mark.asyncio
async def test_e2e_hooks_are_not_registered_by_default(client: AsyncClient):
    creds = await _register_and_login(client, "e2edisabled")

    response = await client.post(
        "/v1/e2e/workflows/budget-guardrails",
        cookies=creds["cookies"],
    )

    assert response.status_code == 404
    # FastAPI 0.141 may expose included-router wrapper entries alongside
    # concrete routes; only concrete routes have a path attribute.
    route_paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/v1/e2e/workflows/budget-guardrails" not in route_paths


@pytest.mark.asyncio
async def test_e2e_budget_guardrail_hook_runs_current_workspace_workflow(client: AsyncClient):
    creds = await _register_and_login(client, "e2ebudget")

    categories = await client.get("/v1/spending/categories", cookies=creds["cookies"])
    assert categories.status_code == 200
    category_id = categories.json()["items"][0]["public_id"]
    month_start = datetime.now(UTC).date().replace(day=1).isoformat()

    budget = await client.post(
        "/v1/spending/budgets",
        json={"category_id": category_id, "amount": "100.00", "start_month": month_start},
        cookies=creds["cookies"],
    )
    assert budget.status_code == 201, budget.text

    account_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Wallet", "account_type": "wallet", "default_currency_code": "USD"},
        cookies=creds["cookies"],
    )
    assert account_res.status_code == 201, account_res.text
    account_id = account_res.json()["public_id"]

    transaction = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": category_id,
            "account_id": account_id,
            "amount": "95.00",
            "type": "expense",
            "occurred_at": datetime.now(UTC).isoformat(),
            "description": "E2E hook budget breach",
        },
        cookies=creds["cookies"],
    )
    assert transaction.status_code == 201, transaction.text

    settings.ENABLE_E2E_TEST_HOOKS = True
    settings.ENV = "local"
    try:
        async with await _e2e_hook_client() as hook_client:
            hook_client.cookies.update(creds["cookies"])
            response = await hook_client.post("/v1/e2e/workflows/budget-guardrails")
    finally:
        settings.ENABLE_E2E_TEST_HOOKS = False

    assert response.status_code == 200, response.text

    todos = await client.get("/v1/todo/", cookies=creds["cookies"])
    assert todos.status_code == 200
    assert any("budget" in item["title"].lower() for item in todos.json()["items"])


@pytest.mark.asyncio
async def test_e2e_recurring_hook_generates_due_transaction(client: AsyncClient):
    creds = await _register_and_login(client, "e2erecur")
    rule_description = "E2E hook subscription"

    categories = await client.get("/v1/spending/categories", cookies=creds["cookies"])
    assert categories.status_code == 200
    category_id = categories.json()["items"][0]["public_id"]

    account_res = await client.post(
        "/v1/finance/accounts",
        json={"name": "Wallet", "account_type": "wallet", "default_currency_code": "USD"},
        cookies=creds["cookies"],
    )
    assert account_res.status_code == 201, account_res.text
    account_id = account_res.json()["public_id"]

    recurrence = await client.post(
        "/v1/spending/recurring",
        json={
            "category_id": category_id,
            "account_id": account_id,
            "amount": "19.99",
            "type": "expense",
            "description": rule_description,
            "frequency": "monthly",
            "interval": 1,
            "anchor_date": "2099-01-01",
        },
        cookies=creds["cookies"],
    )
    assert recurrence.status_code == 201, recurrence.text

    settings.ENABLE_E2E_TEST_HOOKS = True
    settings.ENV = "local"
    try:
        async with await _e2e_hook_client() as hook_client:
            hook_client.cookies.update(creds["cookies"])
            response = await hook_client.post(
                "/v1/e2e/workflows/recurring-transactions",
                json={"description": rule_description},
            )
    finally:
        settings.ENABLE_E2E_TEST_HOOKS = False

    assert response.status_code == 200, response.text
    assert response.json()["generated_count"] == 1

    transactions = await client.get(
        "/v1/spending/transactions?limit=50&offset=0",
        cookies=creds["cookies"],
    )
    assert transactions.status_code == 200
    generated = next(
        item
        for item in transactions.json()["items"]
        if item["description"] == rule_description and item["amount"] == "19.99"
    )
    assert generated["account_id"] == account_id


@pytest.mark.asyncio
async def test_e2e_weekly_summary_hook_returns_generated_summary(client: AsyncClient):
    creds = await _register_and_login(client, "e2eweekly")
    now = datetime.now(UTC)
    week_start = (now.date() - timedelta(days=now.weekday())).isoformat()
    week_end = (datetime.fromisoformat(week_start).date() + timedelta(days=6)).isoformat()

    todo = await client.post(
        "/v1/todo/",
        json={"title": "E2E hook weekly summary task", "priority": "medium"},
        cookies=creds["cookies"],
    )
    assert todo.status_code == 201, todo.text

    settings.ENABLE_E2E_TEST_HOOKS = True
    settings.ENV = "local"
    try:
        async with await _e2e_hook_client() as hook_client:
            hook_client.cookies.update(creds["cookies"])
            response = await hook_client.post(
                "/v1/e2e/workflows/weekly-summary",
                json={"week_start": week_start},
            )
    finally:
        settings.ENABLE_E2E_TEST_HOOKS = False

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["summary_public_id"]
    assert body["week_start"] == week_start
    assert body["week_end"] == week_end

    summaries = await client.get("/v1/summaries/weekly", cookies=creds["cookies"])
    assert summaries.status_code == 200
    assert summaries.json()["items"][0]["public_id"] == body["summary_public_id"]
    assert summaries.json()["items"][0]["todo_summary"]["tasks_created"] == 1

    unread = await client.get("/v1/notifications/unread-count", cookies=creds["cookies"])
    assert unread.status_code == 200
    assert unread.json()["count"] == 1


@pytest.mark.asyncio
async def test_e2e_fx_rate_hook_seeds_rate_used_by_performance_snapshot(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    creds = await _register_and_login(client, "e2efxrate")

    monkeypatch.setattr(settings, "ENABLE_E2E_TEST_HOOKS", True)
    monkeypatch.setattr(settings, "ENV", "local")
    async with await _e2e_hook_client() as hook_client:
        hook_client.cookies.update(creds["cookies"])
        response = await hook_client.post(
            "/v1/e2e/fx-rates",
            json={
                "base_currency_code": "GBP",
                "quote_currency_code": "USD",
                "rate": "1.25",
            },
        )

    assert response.status_code == 200, response.text

    fx_rates = await client.get(
        "/v1/finance/fx-rates?base=GBP&quote=USD",
        cookies=creds["cookies"],
    )
    assert fx_rates.status_code == 200, fx_rates.text
    assert fx_rates.json()["rate"] == "1.2500000000"


@pytest.mark.asyncio
async def test_e2e_portfolio_report_hook_seeds_report_listed_by_workspace(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Seeds the shape the fortnightly cron really writes, read back the way the web app does."""
    creds = await _register_and_login(client, "e2ereport")
    workspaces = await client.get("/v1/platform/workspaces/", cookies=creds["cookies"])
    assert workspaces.status_code == 200, workspaces.text
    workspace_public_id = workspaces.json()["items"][0]["public_id"]

    monkeypatch.setattr(settings, "ENABLE_E2E_TEST_HOOKS", True)
    monkeypatch.setattr(settings, "ENV", "local")
    async with await _e2e_hook_client() as hook_client:
        hook_client.cookies.update(creds["cookies"])
        response = await hook_client.post(
            "/v1/e2e/portfolio-reports",
            json={
                "as_of": "2026-11-13",
                "theme_weights": {"semis_ai": 31.5, "gold": 12.0},
                "harvest_candidates": [
                    {
                        "symbol": "ABC",
                        "account": "IND Money",
                        "qty": 4,
                        "unrealised_loss_inr": 2500.5,
                        "days_to_long_term": 40,
                        "tax_saved_inr": 780.25,
                    }
                ],
                "lrs_headroom_inr": "662922.41",
                "tax_buckets": {"lt_gain_inr": 1000, "lt_tax_inr": 125, "st_gain_inr": 0},
            },
        )
    assert response.status_code == 200, response.text
    seeded = response.json()
    assert seeded["status"] == "ok"

    listed = await client.get(
        "/v1/investing/portfolio-reports",
        params={"workspace_public_id": workspace_public_id, "limit": 1},
        cookies=creds["cookies"],
    )
    assert listed.status_code == 200, listed.text
    item = listed.json()["items"][0]
    assert item["public_id"] == seeded["public_id"]
    assert item["as_of"] == "2026-11-13"
    assert item["lrs_headroom_inr"] == "662922.41"
    assert item["harvest_candidates"][0]["unrealised_loss_inr"] == 2500.5
    assert item["tax_buckets"]["lt_tax_inr"] == 125


@pytest.mark.asyncio
async def test_e2e_portfolio_report_hook_not_registered_by_default(client: AsyncClient):
    creds = await _register_and_login(client, "e2ereportoff")

    response = await client.post(
        "/v1/e2e/portfolio-reports",
        json={"as_of": "2026-11-13"},
        cookies=creds["cookies"],
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_e2e_health_results_hook_seeds_through_the_real_write_path(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    creds = await _register_and_login(client, "e2ehealth")
    monkeypatch.setattr(settings, "ENABLE_E2E_TEST_HOOKS", True)
    monkeypatch.setattr(settings, "ENV", "local")
    async with await _e2e_hook_client() as hook_client:
        hook_client.cookies.update(creds["cookies"])
        response = await hook_client.post(
            "/v1/e2e/health-results",
            json={
                "record": {
                    "kind": "lab_report",
                    "title": "E2E panel",
                    "recorded_on": "2026-11-10",
                    "note": "seeded by the e2e hook",
                },
                "items": [
                    {"metric": "HbA1c", "value": "5.6", "unit": "%", "measured_on": "2026-10-01"},
                    {"metric": "HbA1c", "value": "5.9", "unit": "%", "ref_high": "5.7"},
                ],
            },
        )
        assert response.status_code == 200, response.text
        seeded = response.json()
        assert seeded["created"] == 2 and seeded["record_public_id"]

        bad = await hook_client.post(
            "/v1/e2e/health-results",
            json={"items": [{"metric": "HbA1c", "value": "x"}], "create_missing_metrics": True},
        )
        assert bad.status_code == 200, bad.text  # partial success: the bad item is just not written
        assert bad.json()["created"] == 0

    series = await client.get("/v1/health/metrics/hba1c/series", cookies=creds["cookies"])
    assert series.status_code == 200, series.text
    assert series.json()["total"] == 2
    assert series.json()["points"][0]["in_range"] == "high"
    record = await client.get(
        f"/v1/health/records/{seeded['record_public_id']}", cookies=creds["cookies"]
    )
    assert record.json()["note"] == "seeded by the e2e hook"


@pytest.mark.asyncio
async def test_e2e_health_results_hook_not_registered_by_default(client: AsyncClient):
    creds = await _register_and_login(client, "e2ehealthoff")
    response = await client.post(
        "/v1/e2e/health-results",
        json={"items": [{"metric": "HbA1c", "value": "5.6", "unit": "%"}]},
        cookies=creds["cookies"],
    )
    assert response.status_code == 404


def test_e2e_hooks_production_gating_settings():
    # Should raise error if we try to set ENABLE_E2E_TEST_HOOKS=True in production
    with pytest.raises(
        ValueError, match="ENABLE_E2E_TEST_HOOKS must remain disabled in production"
    ):
        Settings(
            ENV="production",
            SECRET_KEY="production-secret-key-changed-in-production-12345",
            SESSION_SECRET_KEY="production-session-key-changed-in-production-12345",
            GOOGLE_REDIRECT_URI="https://api.example.com/v1/auth/oauth/google/callback",
            GITHUB_REDIRECT_URI="https://api.example.com/v1/auth/oauth/github/callback",
            METRICS_TOKEN="production-metrics-token-changed-in-production-12345",
            COOKIE_SECURE=True,
            COOKIE_DOMAIN=".sajankp.com",
            RATE_LIMIT_STORAGE_URI="redis://localhost:6379/1",
            ENABLE_E2E_TEST_HOOKS=True,
        )

    # Should raise error if we try to set ENABLE_E2E_TEST_HOOKS=True in staging
    with pytest.raises(
        ValueError, match="ENABLE_E2E_TEST_HOOKS must remain disabled in production"
    ):
        Settings(
            ENV="staging",
            SECRET_KEY="staging-secret-key-changed-in-staging-12345",
            SESSION_SECRET_KEY="staging-session-key-changed-in-staging-12345",
            GOOGLE_REDIRECT_URI="https://api.example.com/v1/auth/oauth/google/callback",
            GITHUB_REDIRECT_URI="https://api.example.com/v1/auth/oauth/github/callback",
            METRICS_TOKEN="staging-metrics-token-changed-in-staging-12345",
            COOKIE_SECURE=True,
            COOKIE_DOMAIN=".sajankp.com",
            RATE_LIMIT_STORAGE_URI="redis://localhost:6379/1",
            ENABLE_E2E_TEST_HOOKS=True,
        )

    # Valid config in production with ENABLE_E2E_TEST_HOOKS=False should succeed
    prod_settings = Settings(
        ENV="production",
        SECRET_KEY="production-secret-key-changed-in-production-12345",
        SESSION_SECRET_KEY="production-session-key-changed-in-production-12345",
        GOOGLE_REDIRECT_URI="https://api.example.com/v1/auth/oauth/google/callback",
        GITHUB_REDIRECT_URI="https://api.example.com/v1/auth/oauth/github/callback",
        METRICS_TOKEN="production-metrics-token-changed-in-production-12345",
        COOKIE_SECURE=True,
        COOKIE_DOMAIN=".sajankp.com",
        RATE_LIMIT_STORAGE_URI="redis://localhost:6379/1",
        ENABLE_E2E_TEST_HOOKS=False,
    )
    assert prod_settings.ENABLE_E2E_TEST_HOOKS is False
