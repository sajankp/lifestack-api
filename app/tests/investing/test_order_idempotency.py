"""Order idempotency: dry_run, source_ref dedupe and the possible_duplicate warning (spec-101)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.auth.models import User
from app.config import settings
from app.core.audit import AuditLog
from app.core.database import postgres
from app.core.exceptions import ForbiddenError
from app.finance.models import Account, AccountType
from app.investing.models import (
    CashBalance,
    Holding,
    Instrument,
    InvestingOrder,
    OrderLot,
)
from app.mcp.server import create_mcp_server
from app.platform.models import Workspace, WorkspaceMembership

USER = 31
WS = 41
TABLES = (InvestingOrder, Holding, CashBalance, Instrument, OrderLot, AuditLog)


@pytest.fixture
async def seed(override_database_url, monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.lifestack.test")
    async with postgres.async_session_maker() as session:
        session.add(
            User(
                id=USER,
                email="orders@example.com",
                username="orders",
                hashed_password="x",
                timezone="Asia/Kolkata",
            )
        )
        session.add(Workspace(id=WS, name="Orders WS"))
        session.add(Workspace(id=WS + 1, name="Other Orders WS"))
        await session.flush()
        session.add(WorkspaceMembership(workspace_id=WS, user_id=USER, role="owner"))
        broker = Account(
            workspace_id=WS,
            name="Broker",
            default_currency_code="USD",
            account_type=AccountType.brokerage,
        )
        broker2 = Account(
            workspace_id=WS,
            name="Broker Two",
            default_currency_code="USD",
            account_type=AccountType.brokerage,
        )
        other_broker = Account(
            workspace_id=WS + 1,
            name="Broker",
            default_currency_code="USD",
            account_type=AccountType.brokerage,
        )
        session.add_all([broker, broker2, other_broker])
        await session.flush()
        for account, ws in ((broker, WS), (broker2, WS), (other_broker, WS + 1)):
            session.add(
                CashBalance(
                    workspace_id=ws,
                    user_id=USER,
                    account_id=account.id,
                    balance=Decimal("10000.00"),
                    currency="USD",
                    as_of=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
        await session.commit()
        return {
            "broker": str(broker.public_id),
            "broker2": str(broker2.public_id),
            "other_broker": str(other_broker.public_id),
        }


class _Auth:
    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, session, workspace_id, *, required_scope, tool):
        self.calls.append({"workspace_id": workspace_id, "scope": required_scope, "tool": tool})
        return USER


@pytest.fixture
async def order_tool(seed, monkeypatch):
    auth = _Auth()
    monkeypatch.setattr("app.mcp.security.authorize_workspace", auth)
    tools = {t.name: t.fn for t in await create_mcp_server().list_tools()}

    async def call(**over):
        args = {
            "workspace_id": WS,
            "account_id": seed["broker"],
            "order_type": "buy",
            "symbol": "AAPL",
            "quantity": "2",
            "price_per_unit": "100",
            "currency": "USD",
            "occurred_at": "2026-07-03T10:00:00+00:00",
        }
        args.update(over)
        return await tools["create_investment_order"](**args)

    call.auth = auth  # type: ignore[attr-defined]
    return call


async def _snapshot() -> dict:
    async with postgres.async_session_maker() as session:
        return {
            table.__tablename__: (
                await session.execute(select(func.count()).select_from(table))
            ).scalar_one()
            for table in TABLES
        }


async def _orders(workspace_id=WS):
    async with postgres.async_session_maker() as session:
        return list(
            (
                await session.execute(
                    select(InvestingOrder).where(InvestingOrder.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_dry_run_reports_would_create_and_writes_nothing(order_tool):
    before = await _snapshot()
    res = await order_tool(dry_run=True, brokerage_fee="1.50")  # note: confirmed not needed
    assert res["status"] == "success" and res["dry_run"] is True
    assert res["counts"] == {"create": 1, "duplicate": 0, "possible_duplicate": 0, "invalid": 0}
    assert res["items"] == [
        {
            "index": 0,
            "status": "would_create",
            "match_reason": None,
            "existing_id": None,
            "errors": [],
            "warnings": [],
        }
    ]
    assert "entity_public_id" not in res
    assert (res["gross_amount"], res["net_amount"]) == ("200", "201.50")
    assert await _snapshot() == before, "dry_run must not touch orders/holdings/cash/audit/lots"
    assert await _orders() == []


@pytest.mark.asyncio
async def test_dry_run_authorizes_with_the_write_scope(order_tool):
    await order_tool(dry_run=True)
    assert order_tool.auth.calls == [
        {"workspace_id": WS, "scope": "mcp:write", "tool": "create_investment_order"}
    ]


@pytest.mark.asyncio
async def test_dry_run_is_not_an_unauthenticated_oracle(order_tool, monkeypatch):
    async def deny(*_a, **_k):
        raise ForbiddenError(detail="You do not have access to this workspace")

    monkeypatch.setattr("app.mcp.security.authorize_workspace", deny)
    with pytest.raises(ForbiddenError):
        await order_tool(dry_run=True, workspace_id=WS + 1, source_ref="probe")


@pytest.mark.asyncio
async def test_dry_run_runs_real_validation_without_persisting(order_tool):
    before = await _snapshot()
    broke = await order_tool(dry_run=True, quantity="1000")  # needs 100,000 > 10,000 cash
    assert broke["items"][0]["status"] == "invalid"
    assert "Insufficient cash" in broke["items"][0]["errors"][0]
    assert broke["counts"]["invalid"] == 1
    oversell = await order_tool(dry_run=True, order_type="sell")
    assert oversell["items"][0]["status"] == "invalid"
    assert "Cannot sell" in oversell["items"][0]["errors"][0]
    wrong_ccy = await order_tool(dry_run=True, currency="EUR")
    assert wrong_ccy["items"][0]["status"] == "invalid"
    assert await _snapshot() == before


@pytest.mark.asyncio
async def test_dry_run_and_real_run_agree_on_shape(order_tool):
    dry = await order_tool(dry_run=True, source_ref="shape-1")
    real = await order_tool(source_ref="shape-1", confirmed=True)
    assert dry["items"][0]["status"] == "would_create"
    assert real["status"] == "success" and real["entity_public_id"]
    # After creation the same dry run reports the duplicate with the real ID.
    after = await order_tool(dry_run=True, source_ref="shape-1")
    assert after["items"][0]["status"] == "duplicate"
    assert after["items"][0]["match_reason"] == "source_ref"
    assert after["items"][0]["existing_id"] == real["entity_public_id"]
    assert after["counts"]["duplicate"] == 1


@pytest.mark.asyncio
async def test_unconfirmed_real_run_still_asks_for_confirmation(order_tool):
    before = await _snapshot()
    res = await order_tool()
    assert res["status"] == "needs_confirmation"
    assert await _snapshot() == before
    assert order_tool.auth.calls == []


@pytest.mark.asyncio
async def test_source_ref_retry_returns_the_existing_order_and_writes_once(order_tool):
    first = await order_tool(source_ref="op-7", confirmed=True)
    assert first["status"] == "success" and "idempotent" not in first
    after_first = await _snapshot()
    again = await order_tool(source_ref="op-7", confirmed=True)
    assert again["status"] == "success" and again["idempotent"] is True
    assert again["match_reason"] == "source_ref"
    assert again["existing_id"] == again["entity_public_id"] == first["entity_public_id"]
    assert await _snapshot() == after_first
    assert len(await _orders()) == 1


@pytest.mark.asyncio
async def test_source_ref_is_workspace_scoped(order_tool, seed):
    await order_tool(source_ref="shared", confirmed=True)
    other = await order_tool(
        source_ref="shared", confirmed=True, workspace_id=WS + 1, account_id=seed["other_broker"]
    )
    assert other["status"] == "success" and "idempotent" not in other
    assert len(await _orders()) == 1 and len(await _orders(WS + 1)) == 1


@pytest.mark.asyncio
async def test_identical_order_without_source_ref_is_possible_duplicate_not_recorded(order_tool):
    first = await order_tool(confirmed=True)
    assert first["status"] == "success"
    after_first = await _snapshot()

    again = await order_tool(confirmed=True)
    assert again["status"] == "possible_duplicate"
    assert again["match_reason"] == "possible_duplicate"
    assert again["existing_id"] == first["entity_public_id"]
    assert "allow_duplicate" in again["message"]
    assert await _snapshot() == after_first, "no silent create"
    assert len(await _orders()) == 1  # and the original is untouched, not "skipped" away

    preview = await order_tool(dry_run=True)
    assert preview["items"][0]["status"] == "possible_duplicate"
    assert preview["items"][0]["existing_id"] == first["entity_public_id"]
    assert preview["counts"]["possible_duplicate"] == 1
    assert await _snapshot() == after_first

    forced = await order_tool(confirmed=True, allow_duplicate=True)
    assert forced["status"] == "success" and "idempotent" not in forced
    assert len(await _orders()) == 2


@pytest.mark.asyncio
async def test_possible_duplicate_requires_every_field_to_match(order_tool, seed):
    await order_tool(confirmed=True)
    for override in (
        {"price_per_unit": "101"},
        {"quantity": "3"},
        {"symbol": "MSFT"},
        {"account_id": seed["broker2"]},
        {"occurred_at": "2026-07-04T10:00:00+00:00"},
    ):
        res = await order_tool(dry_run=True, **override)
        assert res["items"][0]["status"] == "would_create", (override, res["items"])
    # Fees differ but the economic identity is the same -> still flagged.
    fee = await order_tool(dry_run=True, brokerage_fee="2")
    assert fee["items"][0]["status"] == "possible_duplicate"
    # Opposite side is a different order (valid: we hold 2 AAPL after the buy).
    sell = await order_tool(dry_run=True, order_type="sell")
    assert sell["items"][0]["status"] == "would_create"
    assert len(await _orders()) == 1


@pytest.mark.asyncio
async def test_possible_duplicate_uses_the_users_local_day(order_tool):
    # 23:30 IST on 3 Jul (18:00Z) and 00:30 IST on 4 Jul (19:00Z) share a UTC date
    # but are different local days -> not duplicates.
    await order_tool(confirmed=True, occurred_at="2026-07-03T18:00:00+00:00")
    next_local_day = await order_tool(dry_run=True, occurred_at="2026-07-03T19:00:00+00:00")
    assert next_local_day["items"][0]["status"] == "would_create"
    same_local_day = await order_tool(dry_run=True, occurred_at="2026-07-03T03:00:00+00:00")
    assert same_local_day["items"][0]["status"] == "possible_duplicate"


@pytest.mark.asyncio
async def test_multiple_existing_matches_do_not_break_the_lookup(order_tool):
    await order_tool(confirmed=True)
    await order_tool(confirmed=True, allow_duplicate=True)
    assert len(await _orders()) == 2
    res = await order_tool(confirmed=True)
    assert res["status"] == "possible_duplicate" and res["possible_duplicate_count"] == 2
    dry = await order_tool(dry_run=True)
    assert dry["items"][0]["status"] == "possible_duplicate"


@pytest.mark.asyncio
async def test_possible_duplicate_ignores_other_workspaces(order_tool, seed):
    await order_tool(confirmed=True, workspace_id=WS + 1, account_id=seed["other_broker"])
    res = await order_tool(dry_run=True)
    assert res["items"][0]["status"] == "would_create"


@pytest.mark.asyncio
async def test_tool_exposes_the_new_parameters(order_tool):
    tools = {t.name: t for t in await create_mcp_server().list_tools()}
    props = tools["create_investment_order"].parameters["properties"]
    assert {"dry_run", "allow_duplicate", "source_ref", "confirmed"} <= set(props)
    for name in ("log_spending_transaction", "log_spending_transactions_batch"):
        assert {"dry_run", "skip_duplicates"} <= set(tools[name].parameters["properties"])
    assert "atomic" in tools["log_spending_transactions_batch"].parameters["properties"]
    for name in ("list_spending_transactions", "find_spending_transactions"):
        assert {"from_date", "to_date", "cursor"} <= set(tools[name].parameters["properties"])


@pytest.mark.asyncio
async def test_source_ref_reused_with_different_details_is_a_conflict_not_a_silent_success(
    order_tool,
):
    first = await order_tool(source_ref="op-conflict", confirmed=True)
    assert first["status"] == "success"
    after_first = await _snapshot()

    changed = await order_tool(source_ref="op-conflict", confirmed=True, quantity="5")

    assert changed["status"] == "error"
    assert "source_ref" in changed["message"]
    assert changed["existing_id"] == first["entity_public_id"]
    assert await _snapshot() == after_first, "the changed order must not be recorded either"
    assert len(await _orders()) == 1

    for override in ({"symbol": "MSFT"}, {"price_per_unit": "101"}, {"order_type": "sell"}):
        res = await order_tool(source_ref="op-conflict", confirmed=True, **override)
        assert res["status"] == "error", override


@pytest.mark.asyncio
async def test_source_ref_replay_with_identical_details_still_succeeds(order_tool):
    first = await order_tool(source_ref="op-same", confirmed=True)
    again = await order_tool(source_ref="op-same", confirmed=True)
    assert again["status"] == "success" and again["idempotent"] is True
    assert again["existing_id"] == first["entity_public_id"]


@pytest.mark.asyncio
async def test_dry_run_reports_a_changed_source_ref_replay_as_invalid(order_tool):
    await order_tool(source_ref="op-dry", confirmed=True)

    preview = await order_tool(dry_run=True, source_ref="op-dry", quantity="9")

    item = preview["items"][0]
    assert item["status"] == "invalid"
    assert item["match_reason"] == "source_ref"
    assert preview["counts"]["invalid"] == 1
    assert len(await _orders()) == 1


@pytest.mark.asyncio
async def test_dry_run_honours_allow_duplicate_like_the_real_run(order_tool):
    await order_tool(confirmed=True)

    without = await order_tool(dry_run=True)
    assert without["items"][0]["status"] == "possible_duplicate"

    forced = await order_tool(dry_run=True, allow_duplicate=True)
    assert forced["items"][0]["status"] == "would_create"
    assert forced["counts"]["possible_duplicate"] == 0

    real = await order_tool(confirmed=True, allow_duplicate=True)
    assert real["status"] == "success", "the dry run must predict the real outcome"
