import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from app.config import settings
from app.mcp.auth import LifestackTokenVerifier
from app.mcp.repository import McpGrantRepository
from app.mcp.server import (
    _add_holding_reporting_valuation,
    _sort_holding_items,
    create_mcp_server,
)


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def getdel(self, key: str):
        return self.values.pop(key, None)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.values[key] = value
        return True

    async def delete(self, key: str):
        return int(self.values.pop(key, None) is not None)


@pytest.mark.anyio
async def test_mcp_oauth_client_and_authorization_code_are_durable_shapes(monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.example.test")
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.test")
    provider = LifestackTokenVerifier()
    provider._redis = FakeRedis()
    assert "mcp:research" in provider.client_registration_options.valid_scopes
    grant_id = uuid.uuid4()

    async def fake_upsert(self, user_id, client_id, client_name, scopes):
        return type("Grant", (), {"public_id": grant_id})()

    monkeypatch.setattr(McpGrantRepository, "upsert", fake_upsert)
    provider.get_routes("/mcp")
    client = OAuthClientInformationFull(
        client_id="client-1",
        redirect_uris=["http://127.0.0.1:43123/callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="mcp:read",
    )

    await provider.register_client(client)
    assert (await provider.get_client("client-1")).client_id == "client-1"

    authorization_url = await provider.authorize(
        client,
        AuthorizationParams(
            state="client-state",
            scopes=["mcp:read"],
            code_challenge="A" * 43,
            redirect_uri="http://127.0.0.1:43123/callback",
            redirect_uri_provided_explicitly=True,
            resource=provider.resource_url,
        ),
    )
    state = parse_qs(urlparse(authorization_url).query)["state"][0]
    assert await provider.get_authorization_request(state) == {
        "client_name": "MCP client",
        "scopes": ["mcp:read"],
    }

    callback_url = await provider.complete_authorization(state, 42, "session-1")
    code = parse_qs(urlparse(callback_url).query)["code"][0]
    authorization_code = await provider.load_authorization_code(client, code)
    assert authorization_code is not None
    assert authorization_code.subject == f"42:session-1:{grant_id}"
    assert await provider.load_authorization_code(client, code) is None


@pytest.mark.anyio
async def test_mcp_oauth_rejects_wrong_resource(monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.example.test")
    provider = LifestackTokenVerifier()
    provider._redis = FakeRedis()
    provider.get_routes("/mcp")
    client = OAuthClientInformationFull(
        client_id="client-1", redirect_uris=["http://127.0.0.1:43123/callback"]
    )

    with pytest.raises(Exception) as exc_info:
        await provider.authorize(
            client,
            AuthorizationParams(
                state=None,
                scopes=["mcp:read"],
                code_challenge="A" * 43,
                redirect_uri="http://127.0.0.1:43123/callback",
                redirect_uri_provided_explicitly=True,
                resource="https://other.example.test/mcp",
            ),
        )
    assert exc_info.value.error_description == "resource must identify this MCP server"


@pytest.mark.anyio
async def test_mcp_exposes_voice_transaction_correction_tools(monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.example.test")
    server = create_mcp_server()
    tools = {tool.name: tool for tool in await server.list_tools()}

    assert {
        "create_todo_task",
        "create_recurring_todo",
        "list_todos",
        "get_todo",
        "update_todo",
        "delete_todo",
        "list_next_due_items",
        "log_spending_transaction",
        "list_spending_transactions",
        "find_spending_transactions",
        "update_spending_transaction",
        "delete_spending_transaction",
        "log_weight",
        "log_medication_event",
        "get_investing_summary",
        "get_account_balances",
        "get_workspace_reference_data",
        "list_workspaces",
        "list_investment_holdings",
        "get_investment_constituents",
        "write_investment_constituent_snapshot",
        "delete_investment_constituent_snapshot",
        "list_investment_dividends",
        "create_investment_dividend",
    }.issubset(tools)
    assert "workspace_id" in tools["find_spending_transactions"].parameters["properties"]
    holdings_properties = tools["list_investment_holdings"].parameters["properties"]
    assert {
        "quantity_state",
        "symbol",
        "account_id",
        "currency",
        "instrument_type",
        "sort_by",
        "sort_direction",
        "valuation_currency",
    }.issubset(holdings_properties)
    assert "workspace_id" in tools["update_spending_transaction"].parameters["properties"]
    assert "confirmed" in tools["delete_spending_transaction"].parameters["properties"]
    assert "confirmed" in tools["write_investment_constituent_snapshot"].parameters["properties"]
    assert "confirmed" in tools["delete_investment_constituent_snapshot"].parameters["properties"]
    assert "confirmed" in tools["create_investment_dividend"].parameters["properties"]

    resources = await server.list_resource_templates()
    resource_templates = {resource.uri_template for resource in resources}
    assert "lifestack://workspaces/{workspace_id}/reference-data" in resource_templates
    resources = await server.list_resources()
    assert any(str(resource.uri) == "lifestack://me/workspaces" for resource in resources)


def test_mcp_holding_reporting_valuation_uses_persisted_price_and_fx():
    data: dict = {}
    holding = SimpleNamespace(
        quantity=Decimal("2"), avg_cost=Decimal("100"), currency="USD"
    )
    price = SimpleNamespace(
        unit_price=Decimal("110"), price_date=date(2026, 8, 21), source="bhavcopy"
    )
    fx = {("USD", "INR"): SimpleNamespace(rate=Decimal("80"))}

    _add_holding_reporting_valuation(
        data,
        holding,
        price,
        "INR",
        fx,
        datetime(2026, 8, 22, tzinfo=UTC),
    )

    assert data["reporting_current_value"] == "17600"
    assert data["reporting_book_value"] == "16000"
    assert data["reporting_gain_loss"] == "1600"
    assert data["valuation_status"] == "current"
    assert data["price_as_of"] == "2026-08-21"
    assert data["price_source"] == "bhavcopy"


def test_mcp_holding_sort_keeps_missing_values_last_and_sorts_numeric_values():
    items = [
        {"public_id": "b", "reporting_current_value": "900"},
        {"public_id": "a", "reporting_current_value": None},
        {"public_id": "c", "reporting_current_value": "1000"},
    ]

    sorted_items = _sort_holding_items(items, "current_value", "desc", True)

    assert [item["public_id"] for item in sorted_items] == ["c", "b", "a"]
