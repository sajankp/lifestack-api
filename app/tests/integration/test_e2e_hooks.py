from datetime import UTC, datetime
from http.cookies import SimpleCookie

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
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
    route_paths = {route.path for route in app.routes}
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
        json={"category_id": category_id, "amount": "100.00", "month_start": month_start},
        cookies=creds["cookies"],
    )
    assert budget.status_code == 201, budget.text

    transaction = await client.post(
        "/v1/spending/transactions",
        json={
            "category_id": category_id,
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

    recurrence = await client.post(
        "/v1/spending/recurring",
        json={
            "category_id": category_id,
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
    assert any(
        item["description"] == rule_description and item["amount"] == "19.99"
        for item in transactions.json()["items"]
    )
