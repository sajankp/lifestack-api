"""Exhaustive test suite for all Lifestack MCP server tools and resources."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.mcp.server import (
    _decimal_string,
    _dividend_response,
    create_mcp_server,
)


class MockAsyncSession:
    """Mock async database session for lightning-fast unit tests."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def begin(self):
        return self

    async def commit(self):
        pass

    async def rollback(self):
        pass


@pytest.fixture(autouse=True)
def mock_mcp_environment(monkeypatch):
    """Set MCP environment and mock DB session maker."""
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.lifestack.test")
    monkeypatch.setattr(
        "app.mcp.server.postgres.async_session_maker",
        lambda: MockAsyncSession(),
    )


@pytest.mark.anyio
async def test_mcp_tool_catalog_contains_all_expected_tools():
    """Verify that create_mcp_server registers every documented tool."""
    server = create_mcp_server()
    tools = await server.list_tools()
    registered_tools = {t.name for t in tools}

    expected_tools = {
        # Todo tools
        "create_todo",
        "list_todos",
        "get_todo_summary",
        "create_todo_task",
        "create_recurring_todo",
        "get_todo",
        "update_todo",
        "delete_todo",
        "list_next_due_items",
        # Finance / Net worth / Budgets
        "get_net_worth",
        "get_spending_budgets",
        "get_account_balances",
        # Spending transactions
        "log_spending_transaction",
        "list_spending_transactions",
        "find_spending_transactions",
        "update_spending_transaction",
        "delete_spending_transaction",
        # Transfers
        "list_transfers",
        "find_transfers",
        "create_transfer",
        "update_transfer",
        "delete_transfer",
        # Investing
        "list_investment_holdings",
        "get_investment_constituents",
        "write_investment_constituent_snapshot",
        "delete_investment_constituent_snapshot",
        "list_investment_dividends",
        "create_investment_dividend",
        "get_investing_summary",
        # Health
        "log_weight",
        "log_medication_event",
        # Workspace discovery
        "get_workspace_reference_data",
        "list_workspaces",
    }

    missing = expected_tools - registered_tools
    assert not missing, f"Missing MCP tools from server: {missing}"
    assert len(expected_tools) == 33


@pytest.mark.anyio
async def test_mcp_resource_catalog_contains_all_expected_resources():
    """Verify static resources and templates are registered."""
    server = create_mcp_server()
    resources = await server.list_resources()
    resource_uris = {str(r.uri) for r in resources}
    assert "lifestack://me/workspaces" in resource_uris

    templates = await server.list_resource_templates()
    template_uris = {t.uri_template for t in templates}
    assert "lifestack://workspaces/{workspace_id}/reference-data" in template_uris


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


def test_decimal_string_helper():
    assert _decimal_string(Decimal("123.45")) == "123.45"
    assert _decimal_string(None) is None


def test_dividend_response_helper():
    div = SimpleNamespace(
        public_id=uuid.uuid4(),
        symbol="AAPL",
        income_type="dividend",
        gross_amount=Decimal("10.00"),
        tax_withheld=Decimal("1.50"),
        net_amount=Decimal("8.50"),
        currency="USD",
        pay_date=date(2026, 9, 15),
        created_at=datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
        external_ref="DIV-1",
        notes="Q3 dividend",
    )
    acc = SimpleNamespace(public_id=uuid.uuid4(), name="Brokerage", default_currency_code="USD")
    res = _dividend_response(div, acc)

    assert res["symbol"] == "AAPL"
    assert res["income_type"] == "dividend"
    assert res["gross_amount"] == "10.00"
    assert res["tax_withheld"] == "1.50"
    assert res["net_amount"] == "8.50"
    assert res["account_name"] == "Brokerage"


# ---------------------------------------------------------------------------
# Unit tests for MCP server tools
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_mcp_todo_tools(monkeypatch):
    """Test create_todo, list_todos, get_todo_summary."""
    server = create_mcp_server()
    tools = await server.list_tools()
    tool_map = {t.name: t.fn for t in tools}

    monkeypatch.setattr("app.mcp.server.authorize_workspace", AsyncMock(return_value=101))

    fake_todo = SimpleNamespace(
        id=1,
        public_id=uuid.uuid4(),
        workspace_id=202,
        user_id=101,
        title="Test Todo",
        description="Description",
        due_date=datetime(2026, 10, 5, 10, 0, tzinfo=UTC),
        priority="medium",
        completed=False,
        completed_at=None,
        created_at=datetime(2026, 10, 4, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 10, 4, 10, 0, tzinfo=UTC),
    )

    with patch("app.mcp.server.TodoService") as mock_todo_svc_cls:
        mock_todo_svc = mock_todo_svc_cls.return_value
        mock_todo_svc.create_todo = AsyncMock(return_value=fake_todo)
        mock_todo_svc.list_todos = AsyncMock(return_value=([fake_todo], 1))
        mock_todo_svc.get_summary_counts = AsyncMock(return_value=(5, 10))
        mock_todo_svc.get_overdue_items = AsyncMock(return_value=[fake_todo])
        mock_todo_svc.get_next_due_items = AsyncMock(return_value=[fake_todo])

        # 1. create_todo
        res_create = await tool_map["create_todo"](
            workspace_id=202,
            title="Test Todo",
            description="Description",
            due_date="2026-10-05T10:00:00Z",
        )
        assert res_create["title"] == "Test Todo"

        # 2. list_todos
        res_list = await tool_map["list_todos"](workspace_id=202, limit=10, offset=0)
        assert res_list["total"] == 1
        assert len(res_list["items"]) == 1

        # 3. get_todo_summary
        res_sum = await tool_map["get_todo_summary"](workspace_id=202)
        assert res_sum["pending"] == 5
        assert res_sum["completed"] == 10
        assert res_sum["overdue_count"] == 1


@pytest.mark.anyio
async def test_mcp_net_worth_and_spending_budgets(monkeypatch):
    """Test get_net_worth and get_spending_budgets tools."""
    server = create_mcp_server()
    tools = await server.list_tools()
    tool_map = {t.name: t.fn for t in tools}

    monkeypatch.setattr("app.mcp.server.authorize_workspace", AsyncMock(return_value=101))

    # 1. get_net_worth
    with patch("app.mcp.server.NetWorthService") as mock_nw_cls:
        mock_nw_svc = mock_nw_cls.return_value
        mock_nw_svc.get_net_worth = AsyncMock(
            return_value={"total_net_worth": "50000.00", "currency": "USD"}
        )
        nw = await tool_map["get_net_worth"](workspace_id=202)
        assert nw["total_net_worth"] == "50000.00"

    # 2. get_spending_budgets
    with patch("app.mcp.server.BudgetService") as mock_b_cls:
        mock_b_svc = mock_b_cls.return_value
        fake_b = SimpleNamespace(
            model_dump=lambda: {"id": 1, "category": "Groceries", "amount": "500.00"}
        )
        mock_b_svc.list_budgets_with_details = AsyncMock(return_value=([fake_b], 1))
        b_res = await tool_map["get_spending_budgets"](workspace_id=202)
        assert b_res["total"] == 1
        assert len(b_res["budgets"]) == 1


@pytest.mark.anyio
async def test_mcp_workspace_discovery_and_resources(monkeypatch):
    """Test list_workspaces, get_workspace_reference_data, and resources."""
    server = create_mcp_server()
    tools = await server.list_tools()
    tool_map = {t.name: t.fn for t in tools}

    monkeypatch.setattr("app.mcp.server.authorize_user", lambda **kwargs: 101)
    monkeypatch.setattr("app.mcp.server.authorize_workspace", AsyncMock(return_value=101))

    mock_choices = [{"id": 202, "name": "Primary Workspace", "role": "owner"}]
    monkeypatch.setattr(
        "app.mcp.server._load_workspace_choices",
        AsyncMock(return_value=mock_choices),
    )

    mock_ref = {
        "categories": ["Food", "Travel"],
        "accounts": ["Checking", "Credit Card"],
        "tags": ["personal"],
        "medications": ["Vitamin D"],
        "timezone": "UTC",
    }
    monkeypatch.setattr(
        "app.mcp.server._load_workspace_reference_data",
        AsyncMock(return_value=mock_ref),
    )

    # 1. list_workspaces tool
    ws_res = await tool_map["list_workspaces"]()
    assert ws_res["workspaces"] == mock_choices

    # 2. get_workspace_reference_data tool
    ref_res = await tool_map["get_workspace_reference_data"](workspace_id=202)
    assert ref_res["categories"] == ["Food", "Travel"]

    # 3. my_workspaces resource
    my_ws_resource = await server.get_resource("lifestack://me/workspaces")
    assert my_ws_resource is not None
    ws_json = await my_ws_resource.fn()
    assert "Primary Workspace" in ws_json

    # 4. workspace_reference_data template resource
    ws_ref_tmpl = await server.get_resource_template(
        "lifestack://workspaces/{workspace_id}/reference-data"
    )
    assert ws_ref_tmpl is not None
    ref_json = await ws_ref_tmpl.fn(workspace_id=202)
    assert "Vitamin D" in ref_json


@pytest.mark.anyio
async def test_mcp_investment_holdings_validation_errors(monkeypatch):
    """Test parameter validation in list_investment_holdings."""
    server = create_mcp_server()
    tools = await server.list_tools()
    fn = {t.name: t.fn for t in tools}["list_investment_holdings"]

    # Invalid quantity_state
    res = await fn(workspace_id=202, quantity_state="invalid_state")
    assert res["status"] == "error"
    assert "quantity_state" in res["message"]

    # Invalid sort_by
    res = await fn(workspace_id=202, sort_by="non_existent_column")
    assert res["status"] == "error"
    assert "sort_by" in res["message"]

    # Invalid sort_direction
    res = await fn(workspace_id=202, sort_direction="upwards")
    assert res["status"] == "error"
    assert "sort_direction" in res["message"]

    # Invalid instrument_type
    res = await fn(workspace_id=202, instrument_type="crypto_token")
    assert res["status"] == "error"
    assert "instrument_type" in res["message"]

    # Invalid account_id (malformed UUID)
    res = await fn(workspace_id=202, account_id="not-a-uuid")
    assert res["status"] == "error"
    assert "Invalid account_id" in res["message"]


@pytest.mark.anyio
async def test_mcp_investment_constituents_tools(monkeypatch):
    """Test get, write, delete constituent snapshots with confirmation gating."""
    server = create_mcp_server()
    tools = await server.list_tools()
    tool_map = {t.name: t.fn for t in tools}

    monkeypatch.setattr("app.mcp.server.authorize_workspace", AsyncMock(return_value=101))

    inst_id = str(uuid.uuid4())

    # 1. get_investment_constituents invalid UUID
    bad_res = await tool_map["get_investment_constituents"](
        workspace_id=202, instrument_public_id="invalid"
    )
    assert bad_res["status"] == "error"

    # 2. write_investment_constituent_snapshot needs confirmation
    preview_write = await tool_map["write_investment_constituent_snapshot"](
        workspace_id=202,
        instrument_public_id=inst_id,
        as_of_date="2026-09-01",
        source="etfdb",
        fetched_at="2026-09-01T12:00:00Z",
        constituents=[{"symbol": "AAPL", "weight": "0.1"}],
        confirmed=False,
    )
    assert preview_write["status"] == "needs_confirmation"
    assert preview_write["needs_confirmation"] is True

    # 3. delete_investment_constituent_snapshot needs confirmation
    preview_del = await tool_map["delete_investment_constituent_snapshot"](
        workspace_id=202,
        instrument_public_id=inst_id,
        as_of_date="2026-09-01",
        source="etfdb",
        confirmed=False,
    )
    assert preview_del["status"] == "needs_confirmation"
    assert preview_del["needs_confirmation"] is True


@pytest.mark.anyio
async def test_mcp_investment_dividends_tools(monkeypatch):
    """Test list_investment_dividends and create_investment_dividend."""
    server = create_mcp_server()
    tools = await server.list_tools()
    tool_map = {t.name: t.fn for t in tools}

    monkeypatch.setattr("app.mcp.server.authorize_workspace", AsyncMock(return_value=101))

    acc_id = str(uuid.uuid4())

    # 1. create_investment_dividend needs confirmation
    preview_div = await tool_map["create_investment_dividend"](
        workspace_id=202,
        account_id=acc_id,
        gross_amount="100.00",
        currency="USD",
        pay_date="2026-09-15",
        symbol="MSFT",
        confirmed=False,
    )
    assert preview_div["status"] == "needs_confirmation"
    assert preview_div["gross_amount"] == "100.00"

    # 2. create_investment_dividend invalid income type
    bad_type = await tool_map["create_investment_dividend"](
        workspace_id=202,
        account_id=acc_id,
        gross_amount="100.00",
        currency="USD",
        pay_date="2026-09-15",
        income_type="invalid_type",
        confirmed=True,
    )
    assert bad_type["status"] == "error"
    assert "income_type must be one of" in bad_type["message"]

    # 3. create_investment_dividend confirmed execution returns valid response and summary
    mock_div = MagicMock()
    mock_div.public_id = uuid.uuid4()
    mock_div.symbol = "MSFT"
    mock_div.income_type = "dividend"
    mock_div.gross_amount = Decimal("100.00")
    mock_div.tax_withheld = Decimal("0.00")
    mock_div.net_amount = Decimal("100.00")
    mock_div.currency = "USD"
    mock_div.pay_date = date(2026, 9, 15)
    mock_div.external_ref = None
    mock_div.notes = None
    mock_div.created_at = datetime.now(UTC)
    mock_div.updated_at = datetime.now(UTC)

    mock_acc = MagicMock()
    mock_acc.public_id = uuid.UUID(acc_id)
    mock_acc.name = "Brokerage"

    mock_service = MagicMock()
    mock_service.create_dividend = AsyncMock(return_value=(mock_div, mock_acc))
    monkeypatch.setattr("app.mcp.server._dividend_service", lambda session: mock_service)

    res_div = await tool_map["create_investment_dividend"](
        workspace_id=202,
        account_id=acc_id,
        gross_amount="100.00",
        currency="USD",
        pay_date="2026-09-15",
        symbol="MSFT",
        confirmed=True,
    )
    assert res_div["status"] == "success"
    assert res_div["entity_type"] == "investment_dividend"
    assert res_div["summary"] == "Recorded dividend income of 100.00 USD."
    assert res_div["item"]["net_amount"] == "100.00"


@pytest.mark.anyio
async def test_mcp_delegated_capture_tools(monkeypatch):
    """Test all capture-delegated tools (spending, transfers, todos, health, summary)."""
    server = create_mcp_server()
    tools = await server.list_tools()
    tool_map = {t.name: t.fn for t in tools}

    called_tools = []

    async def fake_run_capture_tool(workspace_id, required_scope, tool_name, kwargs):
        called_tools.append((workspace_id, required_scope, tool_name, kwargs))
        return {
            "status": "success",
            "tool_name": tool_name,
            "workspace_id": workspace_id,
        }

    monkeypatch.setattr("app.mcp.server._run_capture_tool", fake_run_capture_tool)

    # 1. find_spending_transactions
    res = await tool_map["find_spending_transactions"](workspace_id=202, search="coffee", limit=5)
    assert res["status"] == "success"
    assert called_tools[-1][1] == "mcp:read"
    assert called_tools[-1][2] == "find_spending_transactions"

    # 2. update_spending_transaction
    await tool_map["update_spending_transaction"](
        workspace_id=202, public_id="tx-1", amount="25.00", confirmed=True
    )
    assert called_tools[-1][1] == "mcp:write"
    assert called_tools[-1][2] == "update_spending_transaction"

    # 3. delete_spending_transaction
    await tool_map["delete_spending_transaction"](
        workspace_id=202, public_id="tx-1", confirmed=True
    )
    assert called_tools[-1][1] == "mcp:write"
    assert called_tools[-1][2] == "delete_spending_transaction"

    # 4. list_transfers
    await tool_map["list_transfers"](workspace_id=202, day="2026-10-04")
    assert called_tools[-1][1] == "mcp:read"
    assert called_tools[-1][2] == "list_transfers"

    # 5. find_transfers
    await tool_map["find_transfers"](workspace_id=202, amount="100.00")
    assert called_tools[-1][1] == "mcp:read"
    assert called_tools[-1][2] == "find_transfers"

    # 6. create_transfer
    await tool_map["create_transfer"](
        workspace_id=202,
        from_account_name="Checking",
        to_account_name="Savings",
        amount="500.00",
        confirmed=True,
    )
    assert called_tools[-1][1] == "mcp:write"
    assert called_tools[-1][2] == "create_transfer"

    # 7. update_transfer
    await tool_map["update_transfer"](
        workspace_id=202, public_id="tf-1", amount="600.00", confirmed=True
    )
    assert called_tools[-1][1] == "mcp:write"
    assert called_tools[-1][2] == "update_transfer"

    # 8. delete_transfer
    await tool_map["delete_transfer"](workspace_id=202, public_id="tf-1", confirmed=True)
    assert called_tools[-1][1] == "mcp:write"
    assert called_tools[-1][2] == "delete_transfer"

    # 9. create_todo_task
    await tool_map["create_todo_task"](workspace_id=202, title="Task via capture")
    assert called_tools[-1][1] == "mcp:write"
    assert called_tools[-1][2] == "create_todo_task"

    # 10. create_recurring_todo
    await tool_map["create_recurring_todo"](
        workspace_id=202, title="Weekly review", frequency="weekly"
    )
    assert called_tools[-1][1] == "mcp:write"
    assert called_tools[-1][2] == "create_recurring_todo"

    # 11. get_todo
    await tool_map["get_todo"](workspace_id=202, public_id="td-1")
    assert called_tools[-1][1] == "mcp:read"
    assert called_tools[-1][2] == "get_todo"

    # 12. update_todo
    await tool_map["update_todo"](workspace_id=202, public_id="td-1", completed=True)
    assert called_tools[-1][1] == "mcp:write"
    assert called_tools[-1][2] == "update_todo"

    # 13. delete_todo
    await tool_map["delete_todo"](workspace_id=202, public_id="td-1")
    assert called_tools[-1][1] == "mcp:write"
    assert called_tools[-1][2] == "delete_todo"

    # 14. list_next_due_items
    await tool_map["list_next_due_items"](workspace_id=202, limit=5)
    assert called_tools[-1][1] == "mcp:read"
    assert called_tools[-1][2] == "list_next_due_items"

    # 15. log_spending_transaction
    await tool_map["log_spending_transaction"](
        workspace_id=202, amount="15.50", category_name="Lunch"
    )
    assert called_tools[-1][1] == "mcp:write"
    assert called_tools[-1][2] == "log_spending_transaction"

    # 16. list_spending_transactions
    await tool_map["list_spending_transactions"](workspace_id=202, day="2026-10-04")
    assert called_tools[-1][1] == "mcp:read"
    assert called_tools[-1][2] == "list_spending_transactions"

    # 17. log_weight
    await tool_map["log_weight"](workspace_id=202, weight_kg="72.5")
    assert called_tools[-1][1] == "mcp:write"
    assert called_tools[-1][2] == "log_weight"

    # 18. log_medication_event
    await tool_map["log_medication_event"](workspace_id=202, name="Aspirin", status="taken")
    assert called_tools[-1][1] == "mcp:write"
    assert called_tools[-1][2] == "log_medication_event"

    # 19. get_investing_summary
    await tool_map["get_investing_summary"](workspace_id=202)
    assert called_tools[-1][1] == "mcp:read"
    assert called_tools[-1][2] == "get_investing_summary"

    # 20. get_account_balances
    await tool_map["get_account_balances"](workspace_id=202)
    assert called_tools[-1][1] == "mcp:read"
    assert called_tools[-1][2] == "get_account_balances"
