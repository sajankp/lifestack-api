"""DB-backed tests for the read-only MCP tools of spec-103.

Real Postgres (testcontainers) through the tool functions themselves, with
only ``authorize_workspace`` patched so the tests exercise the actual queries,
workspace scoping, pagination and redaction.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select

from app.auth.models import User
from app.config import settings
from app.core.audit import AuditLog, AuditLogger
from app.core.database import postgres
from app.core.exceptions import ForbiddenError, ValidationError
from app.finance.models import Account, CapitalTransfer, FxRate
from app.health.models import Medication, MedicationEvent, WeightEntry
from app.investing.models import (
    Holding,
    HoldingPrice,
    Instrument,
    InvestingOrder,
    PortfolioSnapshot,
)
from app.mcp.server import create_mcp_server
from app.platform.models import Workspace, WorkspaceMembership

WS = 8101
OTHER_WS = 8102
ALICE = 8101
BOB = 8102


@pytest.fixture(autouse=True)
def _mcp_settings(monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.lifestack.test")


@pytest.fixture
def authorize(monkeypatch):
    mock = AsyncMock(return_value=ALICE)
    monkeypatch.setattr("app.mcp.server.authorize_workspace", mock)
    return mock


async def _tools():
    server = create_mcp_server()
    return {t.name: t.fn for t in await server.list_tools()}


async def _seed_base(session) -> dict[str, Account]:
    session.add_all([
        User(
            id=ALICE, email="alice-reads@example.com", username="alice_reads", hashed_password="x"
        ),
        User(id=BOB, email="bob-reads@example.com", username="bob_reads", hashed_password="x"),
        Workspace(id=WS, name="Reads"),
        Workspace(id=OTHER_WS, name="Other"),
    ])
    await session.flush()
    session.add_all([
        WorkspaceMembership(workspace_id=WS, user_id=ALICE, role="owner"),
        WorkspaceMembership(workspace_id=OTHER_WS, user_id=BOB, role="owner"),
    ])
    accounts = {
        "usd": Account(
            workspace_id=WS, name="US Broker", account_type="brokerage", default_currency_code="USD"
        ),
        "inr": Account(
            workspace_id=WS, name="IN Broker", account_type="brokerage", default_currency_code="INR"
        ),
        "bank": Account(
            workspace_id=WS, name="Bank", account_type="bank", default_currency_code="INR"
        ),
        "other": Account(
            workspace_id=OTHER_WS,
            name="Other Broker",
            account_type="brokerage",
            default_currency_code="USD",
        ),
    }
    session.add_all(accounts.values())
    await session.flush()
    return accounts


def _holding(ws, account, symbol, qty, cost, currency, instrument_id=None):
    return Holding(
        workspace_id=ws,
        user_id=ALICE if ws == WS else BOB,
        account_id=account.id,
        symbol=symbol,
        quantity=Decimal(qty),
        avg_cost=Decimal(cost),
        currency=currency,
        instrument_id=instrument_id,
    )


# --------------------------------------------------------------------------
# reconcile_holdings
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_holdings_end_to_end_buckets_and_scoping(override_database_url, authorize):
    async with postgres.async_session_maker() as session:
        accounts = await _seed_base(session)
        infosys = Instrument(workspace_id=WS, symbol="INFOSYS", name="Infosys", isin="INE009A01021")
        session.add(infosys)
        await session.flush()
        session.add_all([
            _holding(WS, accounts["usd"], "AAPL", "10", "150", "USD"),
            _holding(WS, accounts["usd"], "MSFT", "5", "300", "USD"),
            _holding(WS, accounts["inr"], "OLDINFY", "10", "1500", "INR", infosys.id),
            _holding(WS, accounts["inr"], "ITC", "7", "400", "INR"),
            _holding(WS, accounts["inr"], "ZEROED", "0", "10", "INR"),  # sold out: ignored
            # Another workspace holds the same symbol: must never leak.
            _holding(OTHER_WS, accounts["other"], "ZZLEAK", "99", "1", "USD"),
        ])
        await session.commit()

    tools = await _tools()
    result = await tools["reconcile_holdings"](
        workspace_id=WS,
        external_list=[
            {"symbol": "nasdaq:aapl", "quantity": 10.0, "avg_cost": 150.0, "currency": "USD"},
            {"symbol": "MSFT", "quantity": "5", "avg_cost": "315"},  # +5% cost
            {"symbol": "INFY", "isin": "ine009a01021", "quantity": "12", "avg_cost": "1500"},
            {"symbol": "TSLA", "quantity": "3", "avg_cost": "200", "source": "INDmoney"},
        ],
    )
    assert result["status"] == "success", result
    by_symbol = {i["symbol"]: i for i in result["items"]}
    assert by_symbol["AAPL"]["status"] == "matched"
    assert by_symbol["MSFT"]["status"] == "cost_basis_drift"
    assert by_symbol["INFY"]["status"] == "quantity_drift"  # ISIN matched OLDINFY
    assert by_symbol["INFY"]["lifestack"]["holdings"][0]["account_name"] == "IN Broker"
    assert by_symbol["TSLA"]["status"] == "missing_in_lifestack"
    assert by_symbol["ITC"]["status"] == "missing_at_depository"
    assert "ZEROED" not in by_symbol and "ZZLEAK" not in by_symbol
    assert result["summary"]["matched"] == 1
    assert result["summary"]["missing_at_depository"] == 1
    assert result["summary"]["total_lifestack"] == 4
    assert any("No account_id given" in w for w in result["warnings"])
    authorize.assert_awaited()
    assert authorize.await_args.kwargs["required_scope"] == "mcp:read"
    assert authorize.await_args.kwargs["tool"] == "reconcile_holdings"

    # Scoped to one account: the other account's holdings are out of scope entirely.
    scoped = await tools["reconcile_holdings"](
        workspace_id=WS,
        external_list=[{"symbol": "AAPL", "quantity": "10", "avg_cost": "150"}],
        account_id=str((await _account_public_ids())["usd"]),
    )
    assert scoped["status"] == "success"
    assert {i["status"] for i in scoped["items"]} == {"matched", "missing_at_depository"}
    assert scoped["summary"]["total_lifestack"] == 2
    assert not any("No account_id given" in w for w in scoped["warnings"])


async def _account_public_ids() -> dict[str, uuid.UUID]:
    async with postgres.async_session_maker() as session:
        rows = (await session.execute(select(Account).where(Account.workspace_id == WS))).scalars()
        names = {"US Broker": "usd", "IN Broker": "inr", "Bank": "bank"}
        return {names[a.name]: a.public_id for a in rows}


@pytest.mark.asyncio
async def test_reconcile_holdings_rejects_bad_input_and_foreign_accounts(
    override_database_url, authorize
):
    async with postgres.async_session_maker() as session:
        accounts = await _seed_base(session)
        other_public = accounts["other"].public_id
        await session.commit()
    tools = await _tools()
    row = {"symbol": "AAPL", "quantity": "1"}

    too_many = await tools["reconcile_holdings"](
        workspace_id=WS, external_list=[{"symbol": f"S{i}", "quantity": "1"} for i in range(501)]
    )
    assert too_many["status"] == "error" and "between 1 and 500" in too_many["message"]
    assert (await tools["reconcile_holdings"](workspace_id=WS, external_list=[]))[
        "status"
    ] == "error"
    bad_row = await tools["reconcile_holdings"](workspace_id=WS, external_list=[{"symbol": "X"}])
    assert "row 0" in bad_row["message"]
    bad_tol = await tools["reconcile_holdings"](
        workspace_id=WS, external_list=[row], cost_tolerance_pct="500"
    )
    assert bad_tol["status"] == "error"
    bad_account = await tools["reconcile_holdings"](
        workspace_id=WS, external_list=[row], account_id="not-a-uuid"
    )
    assert bad_account == {"status": "error", "message": "Invalid account_id."}
    # An account that exists but in ANOTHER workspace is "not found", never read.
    foreign = await tools["reconcile_holdings"](
        workspace_id=WS, external_list=[row], account_id=str(other_public)
    )
    assert foreign == {"status": "error", "message": "Account not found in this workspace."}
    foreign_row = await tools["reconcile_holdings"](
        workspace_id=WS, external_list=[{**row, "account_id": str(other_public)}]
    )
    assert foreign_row["status"] == "error" and "not found" in foreign_row["message"]


@pytest.mark.asyncio
async def test_reconcile_query_count_does_not_grow_with_holdings(override_database_url, authorize):
    """No N+1: the number of SQL statements is constant in the holding count."""
    async with postgres.async_session_maker() as session:
        accounts = await _seed_base(session)
        await session.commit()
    tools = await _tools()

    async def run(n: int) -> int:
        async with postgres.async_session_maker() as session:
            instruments = [
                Instrument(workspace_id=WS, symbol=f"Q{n}X{i}", name=f"Q{n}X{i}", isin=None)
                for i in range(n)
            ]
            session.add_all(instruments)
            await session.flush()
            session.add_all([
                _holding(WS, accounts["usd"], f"Q{n}X{i}", "1", "10", "USD", instruments[i].id)
                for i in range(n)
            ])
            await session.commit()
        statements: list[str] = []

        def count(conn, cursor, statement, *args):
            statements.append(statement)

        engine = postgres.engine.sync_engine
        event.listen(engine, "before_cursor_execute", count)
        try:
            res = await tools["reconcile_holdings"](
                workspace_id=WS, external_list=[{"symbol": "NOPE", "quantity": "1"}]
            )
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert res["status"] == "success"
        return len(statements)

    small = await run(3)
    large = await run(40)
    assert large == small
    assert large <= 8


# --------------------------------------------------------------------------
# list_recent_changes
# --------------------------------------------------------------------------


async def _seed_audit(session):
    await _seed_base(session)
    when = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
    rows = []
    for i in range(5):  # identical timestamps: ordering must still be deterministic
        rows.append(
            AuditLog(
                workspace_id=WS,
                actor_id=ALICE,
                action="update" if i % 2 else "create",
                module="investing" if i < 3 else "spending",
                entity_type="holding",
                entity_id=i + 1,
                timestamp=when,
                details={
                    "entity_public_id": f"pub-{i}",
                    "before": None if i % 2 == 0 else {"quantity": "1", "password": "hunter2"},
                    "after": {
                        "quantity": str(i),
                        "api_key": "sk-live-123",
                        "password_hash": "$argon2id$abc",
                        "nested": {"refresh_token": "rt", "client_secret": "cs", "ok": "visible"},
                    },
                    "changed_fields": ["quantity"],
                    "request_id": "req-1",
                },
            )
        )
    rows.append(
        AuditLog(
            workspace_id=WS,
            actor_id=BOB,
            action="delete",
            module="spending",
            entity_type="transaction",
            entity_id=99,
            timestamp=when + timedelta(days=3),
            details={
                "entity_public_id": "pub-late",
                "before": {"a": 1},
                "after": None,
                "changed_fields": [],
            },
        )
    )
    rows.append(
        AuditLog(
            workspace_id=OTHER_WS,
            actor_id=BOB,
            action="create",
            module="investing",
            entity_type="holding",
            entity_id=500,
            timestamp=when,
            details={
                "entity_public_id": "pub-OTHER",
                "before": None,
                "after": {"x": 1},
                "changed_fields": [],
            },
        )
    )
    session.add_all(rows)
    await session.commit()


@pytest.mark.asyncio
async def test_list_recent_changes_pagination_is_complete_and_deterministic(
    override_database_url, authorize
):
    async with postgres.async_session_maker() as session:
        await _seed_audit(session)
    tools = await _tools()

    seen: list[str] = []
    for offset in (0, 2, 4):
        page = await tools["list_recent_changes"](workspace_id=WS, limit=2, offset=offset)
        assert page["status"] == "success"
        assert page["total"] == 6  # the other workspace's row is excluded
        seen.extend(i["public_id"] for i in page["items"])
        assert page["has_more"] is (offset + len(page["items"]) < 6)
    assert len(seen) == len(set(seen)) == 6  # no duplicates, no gaps across pages

    again = await tools["list_recent_changes"](workspace_id=WS, limit=100)
    assert [i["public_id"] for i in again["items"]] == seen
    # Newest first; ties broken by descending id.
    assert again["items"][0]["entity_public_id"] == "pub-late"
    assert [i["entity_public_id"] for i in again["items"][1:]] == [
        "pub-4",
        "pub-3",
        "pub-2",
        "pub-1",
        "pub-0",
    ]
    assert "pub-OTHER" not in {i["entity_public_id"] for i in again["items"]}
    authorize.assert_awaited()
    assert authorize.await_args.kwargs["required_scope"] == "mcp:read"
    assert authorize.await_args.kwargs["tool"] == "list_recent_changes"


@pytest.mark.asyncio
async def test_list_recent_changes_filters_and_actor_names(override_database_url, authorize):
    async with postgres.async_session_maker() as session:
        await _seed_audit(session)
    tools = await _tools()

    by_module = await tools["list_recent_changes"](workspace_id=WS, module="investing")
    assert by_module["total"] == 3
    by_action = await tools["list_recent_changes"](workspace_id=WS, action="delete")
    assert [i["entity_public_id"] for i in by_action["items"]] == ["pub-late"]
    assert by_action["items"][0]["actor_name"] == "bob_reads"
    by_entity = await tools["list_recent_changes"](workspace_id=WS, entity_public_id="pub-2")
    assert by_entity["total"] == 1
    assert by_entity["items"][0]["actor_name"] == "alice_reads"
    assert by_entity["items"][0]["details"]["after"]["quantity"] == "2"
    by_type = await tools["list_recent_changes"](workspace_id=WS, entity_type="transaction")
    assert by_type["total"] == 1
    since = await tools["list_recent_changes"](workspace_id=WS, since="2026-05-02")
    assert [i["entity_public_id"] for i in since["items"]] == ["pub-late"]
    until = await tools["list_recent_changes"](workspace_id=WS, until="2026-05-02T00:00:00Z")
    assert until["total"] == 5
    none = await tools["list_recent_changes"](workspace_id=WS, module="nope")
    assert none["items"] == [] and none["total"] == 0 and none["has_more"] is False

    assert (await tools["list_recent_changes"](workspace_id=WS, since="yesterday"))[
        "status"
    ] == "error"
    swapped = await tools["list_recent_changes"](
        workspace_id=WS, since="2026-06-01", until="2026-05-01"
    )
    assert swapped["status"] == "error"


@pytest.mark.asyncio
async def test_list_recent_changes_really_redacts_secrets(override_database_url, authorize):
    async with postgres.async_session_maker() as session:
        await _seed_audit(session)
    tools = await _tools()
    page = await tools["list_recent_changes"](workspace_id=WS, limit=100)
    blob = repr(page)
    for secret in ("hunter2", "sk-live-123", "$argon2id$abc", "'rt'", "'cs'"):
        assert secret not in blob, secret
    after = next(i for i in page["items"] if i["entity_public_id"] == "pub-3")["details"]["after"]
    assert after["api_key"] == "[REDACTED]"
    assert after["password_hash"] == "[REDACTED]"
    assert after["nested"]["refresh_token"] == "[REDACTED]"
    assert after["nested"]["client_secret"] == "[REDACTED]"
    assert after["nested"]["ok"] == "visible"
    before = next(i for i in page["items"] if i["entity_public_id"] == "pub-3")["details"]["before"]
    assert before["password"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_audit_rows_written_by_the_logger_are_redacted_at_rest_and_on_read(
    override_database_url, authorize
):
    async with postgres.async_session_maker() as session:
        await _seed_base(session)
        await AuditLogger(session).log(
            workspace_id=WS,
            actor_id=ALICE,
            action="update",
            module="auth",
            entity_type="user",
            entity_id=1,
            details={
                "entity_public_id": "u-1",
                "before": {"hashed_password": "H1", "mfa_secret": "S1", "email": "a@b.c"},
                "after": {"hashed_password": "H2", "mfa_secret": "S2", "email": "d@e.f"},
                "changed_fields": ["hashed_password"],
            },
        )
        await session.commit()
    tools = await _tools()
    page = await tools["list_recent_changes"](workspace_id=WS, module="auth")
    details = page["items"][0]["details"]
    assert details["before"]["hashed_password"] == "[REDACTED]"
    assert details["after"]["mfa_secret"] == "[REDACTED]"
    assert details["after"]["email"] == "d@e.f"
    assert "H1" not in repr(page) and "S2" not in repr(page)


@pytest.mark.asyncio
async def test_list_recent_changes_caps_page_size(override_database_url, authorize):
    tools = await _tools()
    for kwargs in ({"limit": 101}, {"limit": 0}, {"offset": -1}):
        with pytest.raises(ValidationError):
            await tools["list_recent_changes"](workspace_id=WS, **kwargs)


# --------------------------------------------------------------------------
# get_weight_history / list_medication_events
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_weight_history_pagination_filters_and_scoping(override_database_url, authorize):
    async with postgres.async_session_maker() as session:
        await _seed_base(session)
        base = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)
        session.add_all([
            WeightEntry(
                workspace_id=WS,
                user_id=ALICE,
                measured_at=base + timedelta(days=d),
                weight_kg=Decimal(f"7{d}.25"),
            )
            for d in range(5)
        ])
        # Two entries sharing one timestamp: tiebreak keeps pages stable.
        session.add_all([
            WeightEntry(
                workspace_id=WS,
                user_id=ALICE,
                measured_at=base - timedelta(days=1),
                weight_kg=Decimal("69.50"),
            ),
            WeightEntry(
                workspace_id=WS,
                user_id=ALICE,
                measured_at=base - timedelta(days=1),
                weight_kg=Decimal("69.60"),
            ),
            WeightEntry(
                workspace_id=OTHER_WS, user_id=BOB, measured_at=base, weight_kg=Decimal("99.99")
            ),
        ])
        await session.commit()
    tools = await _tools()

    seen = []
    for offset in range(0, 7, 3):
        page = await tools["get_weight_history"](workspace_id=WS, limit=3, offset=offset)
        assert page["total"] == 7
        seen.extend(i["public_id"] for i in page["items"])
    assert len(seen) == len(set(seen)) == 7
    first = await tools["get_weight_history"](workspace_id=WS, limit=100)
    assert [i["weight_kg"] for i in first["items"]][:2] == ["74.25", "73.25"]  # newest first, str
    assert "99.99" not in repr(first)
    assert all(isinstance(i["weight_kg"], str) for i in first["items"])

    windowed = await tools["get_weight_history"](
        workspace_id=WS, start="2026-03-02", end="2026-03-03T23:59:59"
    )
    assert [i["weight_kg"] for i in windowed["items"]] == ["72.25", "71.25"]
    assert (await tools["get_weight_history"](workspace_id=WS, start="x"))["status"] == "error"
    assert (
        await tools["get_weight_history"](workspace_id=WS, start="2026-04-01", end="2026-03-01")
    )["status"] == "error"
    assert authorize.await_args.kwargs["required_scope"] == "mcp:read"
    assert authorize.await_args.kwargs["tool"] == "get_weight_history"
    for kwargs in ({"limit": 101}, {"limit": 0}, {"offset": -5}):
        with pytest.raises(ValidationError):
            await tools["get_weight_history"](workspace_id=WS, **kwargs)


@pytest.mark.asyncio
async def test_list_medication_events_filters_pagination_and_scoping(
    override_database_url, authorize
):
    async with postgres.async_session_maker() as session:
        await _seed_base(session)

        def med(ws, user, name):
            return Medication(
                workspace_id=ws,
                user_id=user,
                name=name,
                anchor_date=date(2026, 1, 1),
                times=["08:00"],
            )

        metformin, statin, foreign = (
            med(WS, ALICE, "Metformin"),
            med(WS, ALICE, "Statin"),
            med(OTHER_WS, BOB, "Foreign"),
        )
        session.add_all([metformin, statin, foreign])
        await session.flush()
        base = datetime(2026, 4, 1, 8, 0, tzinfo=UTC)
        events = []
        for d in range(4):
            events.append(
                MedicationEvent(
                    workspace_id=WS,
                    user_id=ALICE,
                    medication_id=metformin.id,
                    scheduled_for=base + timedelta(days=d),
                    status="taken" if d % 2 == 0 else "skipped",
                    taken_at=base if d % 2 == 0 else None,
                    note="n",
                )
            )
        events.append(
            MedicationEvent(
                workspace_id=WS,
                user_id=ALICE,
                medication_id=statin.id,
                scheduled_for=base,
                status="taken",
                taken_at=base,
            )
        )
        events.append(
            MedicationEvent(
                workspace_id=OTHER_WS,
                user_id=BOB,
                medication_id=foreign.id,
                scheduled_for=base,
                status="taken",
                taken_at=base,
            )
        )
        session.add_all(events)
        await session.commit()
        metformin_public = metformin.public_id
        foreign_public = foreign.public_id
    tools = await _tools()

    seen = []
    for offset in (0, 2, 4):
        page = await tools["list_medication_events"](workspace_id=WS, limit=2, offset=offset)
        assert page["total"] == 5
        seen.extend(i["public_id"] for i in page["items"])
    assert len(seen) == len(set(seen)) == 5
    everything = await tools["list_medication_events"](workspace_id=WS, limit=100)
    assert "Foreign" not in repr(everything)
    assert {i["medication_name"] for i in everything["items"]} == {"Metformin", "Statin"}
    assert everything["items"][0]["scheduled_for"] > everything["items"][-1]["scheduled_for"]

    one = await tools["list_medication_events"](
        workspace_id=WS, medication_id=str(metformin_public)
    )
    assert one["total"] == 4 and {i["medication_name"] for i in one["items"]} == {"Metformin"}
    skipped = await tools["list_medication_events"](workspace_id=WS, status="Skipped")
    assert skipped["total"] == 2 and all(i["status"] == "skipped" for i in skipped["items"])
    assert all(i["taken_at"] is None for i in skipped["items"])

    assert (await tools["list_medication_events"](workspace_id=WS, status="missed"))[
        "status"
    ] == "error"
    assert (await tools["list_medication_events"](workspace_id=WS, medication_id="zzz"))[
        "status"
    ] == "error"
    cross = await tools["list_medication_events"](
        workspace_id=WS, medication_id=str(foreign_public)
    )
    assert cross == {"status": "error", "message": "Medication not found in this workspace."}
    assert authorize.await_args.kwargs["required_scope"] == "mcp:read"
    assert authorize.await_args.kwargs["tool"] == "list_medication_events"
    for kwargs in ({"limit": 101}, {"limit": 0}, {"offset": -1}):
        with pytest.raises(ValidationError):
            await tools["list_medication_events"](workspace_id=WS, **kwargs)


# --------------------------------------------------------------------------
# get_performance
# --------------------------------------------------------------------------


def _order(ws, account, symbol, side, qty, price, when, currency, realized=None):
    gross = Decimal(qty) * Decimal(price)
    return InvestingOrder(
        workspace_id=ws,
        user_id=ALICE,
        account_id=account.id,
        order_type=side,
        symbol=symbol,
        quantity=Decimal(qty),
        price_per_unit=Decimal(price),
        gross_amount=gross,
        net_amount=gross,
        currency=currency,
        occurred_at=when,
        realized_gain_loss=realized,
    )


async def _seed_performance(session, *, with_fx: bool, with_snapshots: bool = True):
    accounts = await _seed_base(session)
    now = datetime.now(UTC)
    today = now.date()
    year_ago = now - timedelta(days=365)
    infy = _holding(WS, accounts["inr"], "INFY", "10", "100", "INR")
    aapl = _holding(WS, accounts["usd"], "AAPL", "1", "100", "USD")
    flat = _holding(WS, accounts["inr"], "FLAT", "0", "0", "INR")
    session.add_all([infy, aapl, flat])
    await session.flush()
    session.add_all([
        HoldingPrice(
            workspace_id=WS, holding_id=infy.id, price_date=today, unit_price=Decimal("120")
        ),
        HoldingPrice(
            workspace_id=WS, holding_id=aapl.id, price_date=today, unit_price=Decimal("150")
        ),
        _order(WS, accounts["inr"], "INFY", "buy", "10", "100", year_ago, "INR"),
        _order(WS, accounts["usd"], "AAPL", "buy", "1", "100", now - timedelta(days=200), "USD"),
        # Bought and sold on the same day: a zero-length position -> XIRR undefined.
        _order(WS, accounts["inr"], "FLAT", "buy", "1", "50", now - timedelta(days=30), "INR"),
        _order(
            WS,
            accounts["inr"],
            "FLAT",
            "sell",
            "1",
            "55",
            now - timedelta(days=30),
            "INR",
            realized=Decimal("5"),
        ),
    ])
    if with_fx:
        session.add(
            FxRate(
                workspace_id=None,
                base_currency_code="USD",
                quote_currency_code="INR",
                rate=Decimal("83.0"),
                as_of=now - timedelta(days=3),
                fetched_at=now,
                source="test",
            )
        )
    if with_snapshots:
        session.add_all([
            PortfolioSnapshot(
                workspace_id=WS,
                snapshot_date=today - timedelta(days=20),
                total_value=Decimal("1000.00"),
                total_cost=Decimal("900"),
                holdings_value=Decimal("1000.00"),
                cash_value=Decimal("0"),
                currency_code="INR",
                fx_rates_used={},
            ),
            PortfolioSnapshot(
                workspace_id=WS,
                snapshot_date=today - timedelta(days=10),
                total_value=Decimal("2100.00"),
                total_cost=Decimal("1900"),
                holdings_value=Decimal("2100.00"),
                cash_value=Decimal("0"),
                currency_code="INR",
                fx_rates_used={},
            ),
            # Far outside a 1m window: must only count for period=all.
            PortfolioSnapshot(
                workspace_id=WS,
                snapshot_date=today - timedelta(days=300),
                total_value=Decimal("500.00"),
                total_cost=Decimal("500"),
                holdings_value=Decimal("500.00"),
                cash_value=Decimal("0"),
                currency_code="INR",
                fx_rates_used={},
            ),
            # Another workspace's snapshot must never enter this workspace's TWR.
            PortfolioSnapshot(
                workspace_id=OTHER_WS,
                snapshot_date=today - timedelta(days=15),
                total_value=Decimal("9999.00"),
                total_cost=Decimal("1"),
                holdings_value=Decimal("9999.00"),
                cash_value=Decimal("0"),
                currency_code="INR",
                fx_rates_used={},
            ),
        ])
        session.add(
            CapitalTransfer(
                workspace_id=WS,
                actor_id=ALICE,
                from_module="spending",
                to_module="investing",
                from_account_id=accounts["bank"].id,
                to_account_id=accounts["inr"].id,
                from_currency_code="INR",
                to_currency_code="INR",
                gross_amount=Decimal("1000"),
                net_amount_received=Decimal("1000"),
                occurred_at=now - timedelta(days=10),
            )
        )
    await session.commit()
    return accounts


@pytest.mark.asyncio
async def test_get_performance_xirr_twr_and_inr_conversion(override_database_url, authorize):
    async with postgres.async_session_maker() as session:
        accounts = await _seed_performance(session, with_fx=True)
        inr_public, usd_public = accounts["inr"].public_id, accounts["usd"].public_id
    tools = await _tools()
    res = await tools["get_performance"](workspace_id=WS, period="1m")

    assert res["status"] == "success", res
    assert res["reporting_currency"] == "INR" and res["period"] == "1m"
    assert res["xirr_basis"] == "since_inception"
    assert res["valuation_status"] == "current"
    assert res["fx_rates_used"] == {"USD": "83.0000000000"}

    infy = next(h for h in res["by_holding"] if h["symbol"] == "INFY")
    assert infy["xirr"] == "0.2000" and infy["xirr_pct"] == "20.00"  # 1000 -> 1200 in 365d
    assert infy["invested"] == "1000.00" and infy["market_value"] == "1200.00"
    assert infy["converted"]["market_value"] == "1200.00"
    aapl = next(h for h in res["by_holding"] if h["symbol"] == "AAPL")
    assert aapl["currency"] == "USD" and aapl["xirr_status"] == "ok"
    assert aapl["converted"]["market_value"] == "12450.00"  # 150 USD * 83
    flat = next(h for h in res["by_holding"] if h["symbol"] == "FLAT")
    assert flat["xirr"] is None and flat["xirr_status"] == "not_computable"
    assert flat["is_open"] is False

    accts = {a["account_name"]: a for a in res["by_account"]}
    assert accts["IN Broker"]["converted"]["market_value"] == "1200.00"
    assert accts["US Broker"]["converted"]["market_value"] == "12450.00"
    assert accts["US Broker"]["market_value"] == "150.00"
    assert res["overall"]["converted"]["market_value"] == "13650.00"
    assert res["overall"]["converted"]["currency"] == "INR"
    assert res["overall"]["scope"] == "workspace"
    # Multi-currency workspace with no reporting currency -> overall XIRR needs
    # conversion; it is reported as such, not as an exception or a wrong number.
    assert res["overall"]["xirr"] is None
    assert res["overall"]["xirr_status"] == "conversion_required"
    assert any("Overall XIRR" in w for w in res["warnings"])

    twr = res["time_weighted"]
    assert twr["status"] == "ok" and twr["periods"] == 1  # 1m window drops the 300-day-old snapshot
    # 1000 -> 2100 with a 1000 deposit in the period: 10%, not 110%.
    assert twr["twr_pct"] == "10.00"
    assert twr["net_external_flows"] == "1000.00"
    assert twr["currency"] == "INR" and twr["converted"]["end_value"] == "2100.00"

    full = await tools["get_performance"](workspace_id=WS, period="all")
    assert full["time_weighted"]["periods"] == 2  # only this workspace's snapshots

    only_usd = await tools["get_performance"](workspace_id=WS, account_id=str(usd_public))
    assert [a["account_name"] for a in only_usd["by_account"]] == ["US Broker"]
    assert {h["symbol"] for h in only_usd["by_holding"]} == {"AAPL"}
    assert only_usd["overall"]["scope"] == "account"
    assert only_usd["overall"]["converted"]["market_value"] == "12450.00"
    assert str(inr_public) not in repr(only_usd)

    assert authorize.await_args.kwargs["required_scope"] == "mcp:read"
    assert authorize.await_args.kwargs["tool"] == "get_performance"


@pytest.mark.asyncio
async def test_get_performance_missing_fx_warns_instead_of_zero(override_database_url, authorize):
    async with postgres.async_session_maker() as session:
        await _seed_performance(session, with_fx=False)
    tools = await _tools()
    res = await tools["get_performance"](workspace_id=WS)
    assert res["status"] == "success"
    assert res["valuation_status"] == "conversion_required"
    aapl = next(h for h in res["by_holding"] if h["symbol"] == "AAPL")
    assert aapl["market_value"] == "150.00"
    assert aapl["converted"]["market_value"] is None  # never 0
    assert res["overall"]["converted"]["market_value"] is None
    assert any("No FX rate to INR for ['USD']" in w for w in res["warnings"])
    assert res["fx_rates_used"] == {}
    # INR-native figures are unaffected.
    infy = next(h for h in res["by_holding"] if h["symbol"] == "INFY")
    assert infy["converted"]["market_value"] == "1200.00"


@pytest.mark.asyncio
async def test_get_performance_with_no_data_returns_clean_nulls(override_database_url, authorize):
    async with postgres.async_session_maker() as session:
        await _seed_base(session)
        await session.commit()
    tools = await _tools()
    res = await tools["get_performance"](workspace_id=WS)
    assert res["status"] == "success"
    assert res["by_account"] == [] and res["by_holding"] == []
    assert res["overall"]["xirr"] is None
    assert res["overall"]["xirr_status"] == "not_computable"
    assert res["time_weighted"]["status"] == "insufficient_snapshots"
    assert res["time_weighted"]["twr_pct"] is None
    assert res["valuation_status"] == "current"


@pytest.mark.asyncio
async def test_get_performance_validates_inputs(override_database_url, authorize):
    async with postgres.async_session_maker() as session:
        accounts = await _seed_base(session)
        other_public = accounts["other"].public_id
        await session.commit()
    tools = await _tools()
    assert (await tools["get_performance"](workspace_id=WS, period="7d"))["status"] == "error"
    assert (await tools["get_performance"](workspace_id=WS, reporting_currency="RUPEES"))[
        "status"
    ] == "error"
    assert (await tools["get_performance"](workspace_id=WS, account_id="zzz")) == {
        "status": "error",
        "message": "Invalid account_id.",
    }
    foreign = await tools["get_performance"](workspace_id=WS, account_id=str(other_public))
    assert foreign == {"status": "error", "message": "Account not found in this workspace."}
    lower = await tools["get_performance"](workspace_id=WS, period="YTD", reporting_currency="usd")
    assert lower["status"] == "success" and lower["period"] == "ytd"
    assert lower["reporting_currency"] == "USD"


# --------------------------------------------------------------------------
# authorization
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,kwargs",
    [
        ("reconcile_holdings", {"external_list": [{"symbol": "X", "quantity": "1"}]}),
        ("list_recent_changes", {}),
        ("get_performance", {}),
        ("get_weight_history", {}),
        ("list_medication_events", {}),
    ],
)
async def test_every_new_tool_stops_when_authorization_fails(
    override_database_url, monkeypatch, tool, kwargs
):
    monkeypatch.setattr(
        "app.mcp.server.authorize_workspace",
        AsyncMock(side_effect=ForbiddenError(detail="no access")),
    )
    tools = await _tools()
    with pytest.raises(ForbiddenError):
        await tools[tool](workspace_id=WS, **kwargs)


@pytest.mark.asyncio
async def test_get_performance_for_an_account_with_no_activity_never_shows_workspace_figures(
    override_database_url, authorize
):
    async with postgres.async_session_maker() as session:
        accounts = await _seed_base(session)
        now = datetime.now(UTC)
        infy = _holding(WS, accounts["inr"], "INFY", "10", "100", "INR")
        session.add(infy)
        await session.flush()
        session.add_all([
            HoldingPrice(
                workspace_id=WS,
                holding_id=infy.id,
                price_date=now.date(),
                unit_price=Decimal("120"),
            ),
            _order(
                WS, accounts["inr"], "INFY", "buy", "10", "100", now - timedelta(days=365), "INR"
            ),
        ])
        empty = Account(
            workspace_id=WS,
            name="Empty Broker",
            account_type="brokerage",
            default_currency_code="INR",
        )
        session.add(empty)
        await session.commit()
        empty_public = empty.public_id
    tools = await _tools()

    workspace = await tools["get_performance"](workspace_id=WS)
    res = await tools["get_performance"](workspace_id=WS, account_id=str(empty_public))

    # Precondition: the workspace itself has a computable return (+20% over a year),
    # so a leak of that figure into the empty account would be visible.
    assert workspace["overall"]["xirr"] == "0.2000", workspace["overall"]
    assert res["status"] == "success", res
    overall = res["overall"]
    assert overall["scope"] == "account"
    assert overall["xirr"] is None and overall["xirr_status"] == "not_computable"
    assert overall["annualized_return_pct"] is None
    assert overall["total_return_pct"] is None
    assert res["by_holding"] == []
    assert any("no investing activity" in w.lower() for w in res["warnings"]), res["warnings"]


@pytest.mark.asyncio
async def test_get_performance_flags_that_twr_is_workspace_wide_when_an_account_is_requested(
    override_database_url, authorize
):
    async with postgres.async_session_maker() as session:
        accounts = await _seed_performance(session, with_fx=True)
        usd_public = accounts["usd"].public_id
    tools = await _tools()

    scoped = await tools["get_performance"](workspace_id=WS, account_id=str(usd_public))
    whole = await tools["get_performance"](workspace_id=WS)

    assert any("whole workspace" in w.lower() for w in scoped["time_weighted"]["warnings"])
    assert not any("whole workspace" in w.lower() for w in whole["time_weighted"]["warnings"])
