"""Integration tests for spec-101: batch spend writes, idempotency/dry_run, date ranges.

Card t_8ec81992 acceptance:
  1. ``log_spending_transactions_batch`` with the specified semantics
  2. ``dry_run`` on ``log_spending_transaction`` (order dry_run: test_order_idempotency.py)
  3. ``from_date``/``to_date`` + pagination on ``list_/find_spending_transactions``
  4. multiplicity, dry_run shape, possible_duplicate, atomic batch + edge cases
"""

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, func, select

from app.auth.models import User
from app.capture import spending_batch
from app.capture.agent import execute_agent_tool
from app.capture.tools import AgentTools
from app.config import settings
from app.core.audit import AuditLog
from app.core.database import postgres
from app.finance.models import Account, AccountType, WorkspaceFinanceSetting
from app.mcp.server import create_mcp_server
from app.platform.models import Workspace, WorkspaceMembership
from app.spending.models import (
    SpendingCategory,
    SpendingTag,
    SpendingTransaction,
    TransactionSourceType,
    TransactionType,
)

TZ = "Asia/Kolkata"
WS = 20
USER = 10
DAY = "2026-07-03"  # a past, unambiguous local day


@pytest.fixture
async def seed(override_database_url):
    """User, workspace, categories, default wallet + a second card account."""
    async with postgres.async_session_maker() as session:
        session.add(User(id=USER, email="b@example.com", username="batch", hashed_password="x"))
        session.add(Workspace(id=WS, name="Batch WS"))
        session.add(Workspace(id=WS + 1, name="Other WS"))
        await session.flush()
        session.add(WorkspaceMembership(workspace_id=WS, user_id=USER, role="owner"))
        food = SpendingCategory(workspace_id=WS, name="food", normalized_name="food")
        other = SpendingCategory(workspace_id=WS, name="other", normalized_name="other")
        other_ws_cat = SpendingCategory(workspace_id=WS + 1, name="food", normalized_name="food")
        session.add_all([food, other, other_ws_cat])
        wallet = Account(
            workspace_id=WS,
            name="Everyday Wallet",
            default_currency_code="USD",
            account_type=AccountType.wallet,
        )
        card = Account(
            workspace_id=WS,
            name="Visa Card",
            default_currency_code="USD",
            account_type=AccountType.card,
        )
        other_wallet = Account(
            workspace_id=WS + 1,
            name="Everyday Wallet",
            default_currency_code="USD",
            account_type=AccountType.wallet,
        )
        session.add_all([wallet, card, other_wallet])
        await session.flush()
        session.add(WorkspaceFinanceSetting(workspace_id=WS, default_spending_account_id=wallet.id))
        await session.commit()
        return {
            "food": food,
            "other": other,
            "wallet": wallet,
            "card": card,
            "other_ws": {"cat": other_ws_cat, "wallet": other_wallet},
        }


def _chai(**over):
    item = {
        "amount": "50.00",
        "category_name": "food",
        "description": "Chai",
        "account_name": "Everyday Wallet",
        "occurred_at": DAY,
    }
    item.update(over)
    return item


async def _batch(items, *, tz=TZ, **flags):
    return await execute_agent_tool(
        name="log_spending_transactions_batch",
        args={"items": items, **flags},
        user_id=USER,
        workspace_id=WS,
        user_timezone=tz,
    )


async def _single(tz=TZ, **args):
    return await execute_agent_tool(
        name="log_spending_transaction",
        args=args,
        user_id=USER,
        workspace_id=WS,
        user_timezone=tz,
    )


async def _rows(workspace_id=WS):
    async with postgres.async_session_maker() as session:
        return list(
            (
                await session.execute(
                    select(SpendingTransaction)
                    .where(SpendingTransaction.workspace_id == workspace_id)
                    .order_by(SpendingTransaction.id)
                )
            )
            .scalars()
            .all()
        )


async def _counts():
    """Row counts of every table a spending write can touch."""
    async with postgres.async_session_maker() as session:
        return {
            "tx": (await session.execute(select(func.count(SpendingTransaction.id)))).scalar_one(),
            "tags": (await session.execute(select(func.count(SpendingTag.id)))).scalar_one(),
            "audit": (await session.execute(select(func.count(AuditLog.id)))).scalar_one(),
        }


async def _insert_tx(seed, *, occurred_at, description="seed", amount="10.00", workspace=WS):
    """Insert a ledger row directly at an exact instant."""
    async with postgres.async_session_maker() as session:
        if workspace == WS:
            category_id, account_id = seed["food"].id, seed["wallet"].id
        else:
            category_id, account_id = (
                seed["other_ws"]["cat"].id,
                seed["other_ws"]["wallet"].id,
            )
        tx = SpendingTransaction(
            workspace_id=workspace,
            user_id=USER,
            category_id=category_id,
            account_id=account_id,
            amount=Decimal(amount),
            type=TransactionType.expense,
            occurred_at=occurred_at,
            description=description,
            source_type=TransactionSourceType.manual,
        )
        session.add(tx)
        await session.commit()
        return tx.public_id


# ---------------------------------------------------------------- batch: basics


@pytest.mark.asyncio
async def test_batch_creates_items_and_reports_per_item_results(seed):
    res = await _batch([
        _chai(tags=["Morning", "street food"]),
        _chai(amount="120.00", category_name="other", description="Bus pass", source_ref="b-1"),
    ])
    assert res["status"] == "success", res
    assert res["dry_run"] is False and res["atomic"] is False
    assert res["counts"] == {"create": 2, "duplicate": 0, "possible_duplicate": 0, "invalid": 0}
    assert [i["index"] for i in res["items"]] == [0, 1]
    for item in res["items"]:
        assert item["status"] == "created"
        assert item["match_reason"] is None and item["existing_id"] is None
        assert item["errors"] == [] and item["entity_public_id"]
    rows = await _rows()
    assert {(str(r.amount), r.source_ref) for r in rows} == {("50.00", None), ("120.00", "b-1")}
    assert all(r.source_type == TransactionSourceType.voice_agent for r in rows)
    assert all(isinstance(r.amount, Decimal) for r in rows)


@pytest.mark.asyncio
async def test_batch_uses_default_account_and_falls_back_to_other_category(seed):
    res = await _batch([_chai(account_name=None, category_name="nonexistent")])
    assert res["status"] == "success", res
    item = res["items"][0]
    assert item["status"] == "created"
    assert any("not found" in w for w in item["warnings"])
    (row,) = await _rows()
    assert row.account_id == seed["wallet"].id and row.category_id == seed["other"].id


@pytest.mark.asyncio
async def test_batch_cap_is_50_with_clear_error_and_nothing_written(seed):
    before = await _counts()
    res = await _batch([_chai(description=f"Item {i}") for i in range(51)])
    assert res["status"] == "error"
    assert "50" in res["message"] and "51" in res["message"]
    assert res["items"] == []
    assert await _counts() == before

    # Exactly 50 is accepted.
    ok = await _batch([_chai(description=f"Item {i}") for i in range(50)])
    assert ok["status"] == "success" and ok["counts"]["create"] == 50
    assert len(await _rows()) == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("items", [[], None, "nope"])
async def test_batch_rejects_missing_or_non_list_items(seed, items):
    res = await _batch(items)  # type: ignore[arg-type]
    assert res["status"] == "error"
    assert await _rows() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"amount": "abc"}, "invalid amount"),
        ({"amount": "NaN"}, "finite"),
        ({"amount": "Infinity"}, "finite"),
        ({"amount": "-5"}, "greater than zero"),
        ({"amount": "0"}, "greater than zero"),
        ({"amount": "10.005"}, "2 decimal"),
        ({"amount": None}, "required"),
        ({"transaction_type": "transfer"}, "transaction_type"),
        ({"occurred_at": "not-a-date"}, "occurred_at"),
        ({"occurred_at": "2999-01-01"}, "future"),
        ({"account_name": "No Such Account"}, "No spending account"),
        ({"sourceref": "typo"}, "unknown field"),
        ({"tags": "morning"}, "tags"),
        ({"allow_duplicate": "yes"}, "boolean"),
        ({"description": "x" * 501}, "500"),
    ],
)
async def test_batch_flags_invalid_items_without_failing_the_others(seed, override, fragment):
    good = _chai(description="Valid one")
    bad = _chai(**override)
    res = await _batch([good, bad])
    assert res["status"] == "success"
    assert res["counts"]["invalid"] == 1 and res["counts"]["create"] == 1
    assert res["items"][0]["status"] == "created"
    assert res["items"][1]["status"] == "invalid"
    assert fragment in " ".join(res["items"][1]["errors"])
    assert len(await _rows()) == 1


@pytest.mark.asyncio
async def test_batch_does_not_accept_future_date_but_clamps_today_to_now(seed):
    today = datetime.now(ZoneInfo(TZ)).date().isoformat()
    res = await _batch([_chai(occurred_at=today)])
    assert res["items"][0]["status"] == "created"
    (row,) = await _rows()
    assert row.occurred_at <= datetime.now(UTC)


# ------------------------------------------------------- batch: source_ref dedupe


@pytest.mark.asyncio
async def test_source_ref_exact_match_returns_existing_id_and_retry_is_idempotent(seed):
    first = await _single(
        amount="120.00",
        category_name="other",
        description="Bus pass",
        account_name="Everyday Wallet",
        occurred_at=DAY,
        source_ref="ext-bus-001",
    )
    assert first["status"] == "success"
    existing_id = first["entity_public_id"]

    batch = [
        _chai(
            amount="120.00", category_name="other", description="Bus pass", source_ref="ext-bus-001"
        ),
        _chai(description="Fresh", source_ref="ext-new"),
    ]
    res = await _batch(batch)
    assert res["counts"] == {"create": 1, "duplicate": 1, "possible_duplicate": 0, "invalid": 0}
    dup = res["items"][0]
    assert (dup["status"], dup["match_reason"], dup["existing_id"]) == (
        "duplicate",
        "source_ref",
        existing_id,
    )
    assert len(await _rows()) == 2

    # Retrying the whole batch (e.g. after a timeout) adds nothing.
    again = await _batch(batch)
    assert again["counts"] == {"create": 0, "duplicate": 2, "possible_duplicate": 0, "invalid": 0}
    assert len(await _rows()) == 2


@pytest.mark.asyncio
async def test_source_ref_is_scoped_to_the_workspace(seed):
    await _insert_tx(seed, occurred_at=datetime(2026, 7, 3, 6, 0, tzinfo=UTC), workspace=WS + 1)
    async with postgres.async_session_maker() as session:
        row = (
            (
                await session.execute(
                    select(SpendingTransaction).where(SpendingTransaction.workspace_id == WS + 1)
                )
            )
            .scalars()
            .one()
        )
        row.source_ref = "shared-ref"
        row.source_type = TransactionSourceType.voice_agent
        await session.commit()
    res = await _batch([_chai(source_ref="shared-ref")])
    assert res["items"][0]["status"] == "created"  # other workspace's ref is invisible


@pytest.mark.asyncio
async def test_source_ref_repeated_inside_one_batch(seed):
    res = await _batch([
        _chai(source_ref="dup-1"),
        _chai(source_ref="dup-1"),  # identical payload -> duplicate of item 0
        _chai(amount="99.00", source_ref="dup-1"),  # changed payload -> invalid
    ])
    first, repeat, changed = res["items"]
    assert first["status"] == "created"
    assert repeat["status"] == "duplicate" and repeat["match_reason"] == "source_ref"
    assert repeat["duplicate_of_index"] == 0
    assert repeat["existing_id"] == first["entity_public_id"]
    assert changed["status"] == "invalid" and "different payload" in changed["errors"][0]
    assert len(await _rows()) == 1


@pytest.mark.asyncio
async def test_reused_source_ref_with_changed_payload_is_an_error_not_a_silent_accept(seed):
    await _batch([_chai(source_ref="op-1")])
    res = await _batch([_chai(amount="51.00", source_ref="op-1")])
    item = res["items"][0]
    assert item["status"] == "invalid" and item["match_reason"] == "source_ref"
    assert item["existing_id"] and "different" in item["errors"][0]
    (row,) = await _rows()
    assert row.amount == Decimal("50.00")  # original untouched


# --------------------------------------------- batch: natural key + multiplicity


@pytest.mark.asyncio
async def test_multiplicity_ledger_has_one_batch_has_two_creates_one_skips_one(seed):
    first = await _single(**_chai())
    assert first["status"] == "success"
    res = await _batch([_chai(), _chai()], skip_duplicates=True)
    assert res["status"] == "success"
    assert res["counts"] == {"create": 1, "duplicate": 1, "possible_duplicate": 0, "invalid": 0}
    dup = next(i for i in res["items"] if i["status"] == "duplicate")
    assert dup["match_reason"] == "natural_key"
    assert dup["existing_id"] == first["entity_public_id"]
    assert len([r for r in await _rows() if r.amount == Decimal("50.00")]) == 2


@pytest.mark.asyncio
async def test_two_genuine_identical_purchases_are_not_collapsed_when_ledger_is_empty(seed):
    res = await _batch([_chai(), _chai(), _chai()], skip_duplicates=True)
    assert res["counts"]["create"] == 3 and res["counts"]["duplicate"] == 0
    assert len(await _rows()) == 3


@pytest.mark.asyncio
async def test_replaying_a_batch_is_idempotent_with_skip_duplicates(seed):
    batch = [_chai(), _chai(), _chai(description="Samosa", amount="30.00")]
    first = await _batch(batch, skip_duplicates=True)
    assert first["counts"]["create"] == 3
    second = await _batch(batch, skip_duplicates=True)
    assert second["counts"] == {"create": 0, "duplicate": 3, "possible_duplicate": 0, "invalid": 0}
    assert len(await _rows()) == 3
    # Each replayed item maps to a distinct existing row (one-to-one).
    ids = [i["existing_id"] for i in second["items"][:2]]
    assert len(set(ids)) == 2


@pytest.mark.asyncio
async def test_without_skip_duplicates_a_natural_match_is_possible_duplicate_not_written(seed):
    await _single(**_chai())
    res = await _batch([_chai()])
    item = res["items"][0]
    assert item["status"] == "possible_duplicate" and item["match_reason"] == "natural_key"
    assert item["existing_id"]
    assert res["counts"]["possible_duplicate"] == 1 and res["counts"]["create"] == 0
    assert len(await _rows()) == 1  # no silent skip *and* no silent create

    forced = await _batch([_chai(allow_duplicate=True)])
    assert forced["items"][0]["status"] == "created"
    assert len(await _rows()) == 2


@pytest.mark.asyncio
async def test_natural_key_matching_normalises_description_and_amount_scale(seed):
    await _single(**_chai(description="Chai"))
    res = await _batch(
        [_chai(description="  CHAI   ", amount="50")],
        skip_duplicates=True,
    )
    assert res["items"][0]["status"] == "duplicate"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"amount": "51.00"},
        {"description": "Coffee"},
        {"account_name": "Visa Card"},
        {"transaction_type": "income"},
        {"occurred_at": "2026-07-04"},
    ],
)
async def test_any_natural_key_component_differing_means_not_a_duplicate(seed, override):
    await _single(**_chai())
    res = await _batch([_chai(**override)], skip_duplicates=True)
    assert res["items"][0]["status"] == "created", res


@pytest.mark.asyncio
async def test_natural_key_uses_the_users_local_day_across_midnight(seed):
    # 23:30 IST on 3 July is 18:00 UTC on 3 July; 00:30 IST on 4 July is 19:00 UTC on 3 July.
    # Same UTC date, different local days.
    await _batch([_chai(occurred_at="2026-07-03T23:30:00+05:30")])
    next_day = await _batch([_chai(occurred_at="2026-07-04T00:30:00+05:30")], skip_duplicates=True)
    assert next_day["items"][0]["status"] == "created"
    same_day = await _batch([_chai(occurred_at="2026-07-03T08:00:00+05:30")], skip_duplicates=True)
    assert same_day["items"][0]["status"] == "duplicate"


@pytest.mark.asyncio
async def test_natural_key_ignores_other_workspaces(seed):
    await _insert_tx(
        seed,
        occurred_at=datetime(2026, 7, 3, 6, 30, tzinfo=UTC),
        description="Chai",
        amount="50.00",
        workspace=WS + 1,
    )
    res = await _batch([_chai()], skip_duplicates=True)
    assert res["items"][0]["status"] == "created"


@pytest.mark.asyncio
async def test_source_ref_items_skip_the_natural_key_check(seed):
    await _single(**_chai())
    res = await _batch([_chai(source_ref="distinct-purchase")], skip_duplicates=True)
    assert res["items"][0]["status"] == "created"


@pytest.mark.asyncio
async def test_natural_key_lookup_does_not_query_per_item(seed):
    """50 distinct items -> a constant number of spending_transactions SELECTs."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "FROM spending_transactions" in statement and statement.lstrip().startswith("SELECT"):
            statements.append(statement)

    engine = postgres.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        items = [
            _chai(
                description=f"Item {i}",
                amount=f"{i + 1}.00",
                source_ref=f"r-{i}" if i % 2 else None,
            )
            for i in range(50)
        ]
        res = await _batch(items, dry_run=True, skip_duplicates=True)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert res["counts"]["create"] == 50
    assert len(statements) <= 2, f"expected O(1) lookups, saw {len(statements)}"


# ---------------------------------------------------------------- batch: dry_run


@pytest.mark.asyncio
async def test_batch_dry_run_has_the_real_run_shape_minus_ids_and_writes_nothing(seed):
    await _single(**_chai(source_ref="known"))
    items = [
        _chai(description="New thing", amount="12.00"),
        _chai(source_ref="known"),
        _chai(amount="abc"),
    ]
    before = await _counts()
    dry = await _batch(items, dry_run=True)
    assert await _counts() == before, "dry_run must not write rows, tags or audit entries"

    real = await _batch(items)
    assert set(dry) == set(real)  # same top-level structure
    assert dry["dry_run"] is True and real["dry_run"] is False
    assert dry["counts"] == real["counts"]
    for d, r in zip(dry["items"], real["items"], strict=True):
        assert set(d) <= set(r)
        assert set(r) - set(d) <= {"entity_public_id"}  # only created IDs differ
        assert "entity_public_id" not in d
        assert (d["index"], d["match_reason"], d["existing_id"]) == (
            r["index"],
            r["match_reason"],
            r["existing_id"],
        )
    assert [i["status"] for i in dry["items"]] == ["would_create", "duplicate", "invalid"]
    assert [i["status"] for i in real["items"]] == ["created", "duplicate", "invalid"]


@pytest.mark.asyncio
async def test_batch_dry_run_does_not_create_tags(seed):
    before = await _counts()
    res = await _batch([_chai(tags=["brand-new-tag"])], dry_run=True)
    assert res["items"][0]["status"] == "would_create"
    assert (await _counts())["tags"] == before["tags"]


# ---------------------------------------------------------------- batch: atomic


@pytest.mark.asyncio
async def test_atomic_batch_with_an_invalid_item_persists_nothing(seed):
    before = await _counts()
    res = await _batch([_chai(), _chai(amount="not-a-number")], atomic=True)
    assert res["status"] == "error" and res["persisted"] is False
    assert res["counts"]["invalid"] == 1 and res["counts"]["create"] == 0
    assert [i["status"] for i in res["items"]] == ["not_created", "invalid"]
    assert await _counts() == before


@pytest.mark.asyncio
async def test_atomic_batch_with_a_possible_duplicate_persists_nothing(seed):
    await _single(**_chai())
    before = await _counts()
    res = await _batch([_chai(description="Fresh"), _chai()], atomic=True)
    assert res["status"] == "error"
    assert res["items"][1]["status"] == "possible_duplicate"
    assert await _counts() == before


@pytest.mark.asyncio
async def test_atomic_batch_success_and_idempotent_retry(seed):
    batch = [_chai(description="A"), _chai(description="B", source_ref="atomic-b")]
    res = await _batch(batch, atomic=True)
    assert res["status"] == "success" and res["counts"]["create"] == 2
    assert len(await _rows()) == 2
    # All-duplicates retry is a success with nothing created, not an error.
    again = await _batch(batch, atomic=True, skip_duplicates=True)
    assert again["status"] == "success"
    assert again["counts"] == {"create": 0, "duplicate": 2, "possible_duplicate": 0, "invalid": 0}
    assert len(await _rows()) == 2


@pytest.mark.asyncio
async def test_atomic_batch_rolls_back_rows_already_flushed_when_a_later_write_fails(
    seed, monkeypatch
):
    calls = {"n": 0}
    real_create = AgentTools._create_batch_transaction

    async def flaky(self, item, source_type, tag_cache):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom")
        return await real_create(self, item, source_type, tag_cache)

    monkeypatch.setattr(AgentTools, "_create_batch_transaction", flaky)
    before = await _counts()
    async with postgres.async_session_maker() as session:
        tools = AgentTools(session, USER, WS, user_timezone=TZ, source_channel="mcp_agent")
        res = await tools.log_spending_transactions_batch(
            [_chai(description=f"Item {i}", tags=["t"]) for i in range(4)], atomic=True
        )
        assert res["status"] == "error" and res["persisted"] is False
        assert "rolled back" in res["message"]
        await session.commit()  # even a stray commit must find nothing to persist
    assert calls["n"] == 3  # stopped at the failure
    assert await _counts() == before, "first two items (and their tags/audit rows) must be gone"


@pytest.mark.asyncio
async def test_non_atomic_batch_keeps_good_items_when_one_write_fails(seed, monkeypatch):
    real_create = AgentTools._create_batch_transaction

    async def flaky(self, item, source_type, tag_cache):
        if item.description == "explode":
            await real_create(self, item, source_type, tag_cache)  # write, then fail
            raise RuntimeError("late failure")
        return await real_create(self, item, source_type, tag_cache)

    monkeypatch.setattr(AgentTools, "_create_batch_transaction", flaky)
    async with postgres.async_session_maker() as session:
        tools = AgentTools(session, USER, WS, user_timezone=TZ, source_channel="mcp_agent")
        res = await tools.log_spending_transactions_batch([
            _chai(description="ok-1"),
            _chai(description="explode"),
            _chai(description="ok-2"),
        ])
        await session.commit()
    assert [i["status"] for i in res["items"]] == ["created", "invalid", "created"]
    assert res["counts"] == {"create": 2, "duplicate": 0, "possible_duplicate": 0, "invalid": 1}
    # The failing item's savepoint was rolled back; the other two survive.
    assert sorted(r.description for r in await _rows()) == ["ok-1", "ok-2"]


# ------------------------------------------------- single tool: dry_run / replay


@pytest.mark.asyncio
async def test_single_dry_run_returns_batch_shape_no_ids_and_writes_nothing(seed):
    before = await _counts()
    res = await _single(**_chai(description="Samosa", tags=["snack"]), dry_run=True)
    assert res["status"] == "success" and res["dry_run"] is True
    assert "entity_public_id" not in res
    assert res["counts"] == {"create": 1, "duplicate": 0, "possible_duplicate": 0, "invalid": 0}
    (item,) = res["items"]
    assert item == {
        "index": 0,
        "status": "would_create",
        "match_reason": None,
        "existing_id": None,
        "errors": [],
        "warnings": [],
    }
    assert await _counts() == before


@pytest.mark.asyncio
async def test_single_dry_run_reports_source_ref_duplicate_and_conflict(seed):
    first = await _single(**_chai(source_ref="s-1"))
    dup = await _single(**_chai(source_ref="s-1"), dry_run=True)
    assert dup["items"][0]["status"] == "duplicate"
    assert dup["items"][0]["match_reason"] == "source_ref"
    assert dup["items"][0]["existing_id"] == first["entity_public_id"]
    changed = await _single(**_chai(source_ref="s-1", amount="77.00"), dry_run=True)
    assert changed["items"][0]["status"] == "invalid"
    assert "different" in changed["items"][0]["errors"][0]
    assert len(await _rows()) == 1


@pytest.mark.asyncio
async def test_single_dry_run_flags_possible_duplicate_or_duplicate_with_skip(seed):
    first = await _single(**_chai())
    possible = await _single(**_chai(), dry_run=True)
    assert possible["items"][0]["status"] == "possible_duplicate"
    assert possible["counts"]["possible_duplicate"] == 1
    skip = await _single(**_chai(), dry_run=True, skip_duplicates=True)
    assert skip["items"][0]["status"] == "duplicate"
    assert skip["items"][0]["match_reason"] == "natural_key"
    assert skip["items"][0]["existing_id"] == first["entity_public_id"]
    forced = await _single(**_chai(), dry_run=True, allow_duplicate=True)
    assert forced["items"][0]["status"] == "would_create"
    assert len(await _rows()) == 1


@pytest.mark.asyncio
async def test_single_dry_run_applies_the_same_validation_as_a_real_run(seed):
    bad = await _single(**_chai(amount="x"), dry_run=True)
    assert bad["status"] == "error"
    future = await _single(**_chai(occurred_at="2999-01-01"), dry_run=True)
    assert future["status"] == "error" and "future" in future["message"]


@pytest.mark.asyncio
async def test_single_source_ref_retry_returns_existing_record_without_a_new_row(seed):
    first = await _single(**_chai(source_ref="retry-1"))
    again = await _single(**_chai(source_ref="retry-1"))
    assert again["status"] == "success"
    assert again["idempotent"] is True and again["match_reason"] == "source_ref"
    assert again["existing_id"] == again["entity_public_id"] == first["entity_public_id"]
    assert len(await _rows()) == 1


@pytest.mark.asyncio
async def test_single_skip_duplicates_reports_instead_of_refusing(seed):
    first = await _single(**_chai())
    refused = await _single(**_chai())
    assert refused["status"] == "error" and refused["duplicate_detected"] is True
    skipped = await _single(**_chai(), skip_duplicates=True)
    assert skipped["status"] == "success" and skipped["duplicate"] is True
    assert skipped["existing_id"] == first["entity_public_id"]
    assert len(await _rows()) == 1


# --------------------------------------------------- list / find: date ranges


async def _list(tz=TZ, **args):
    return await execute_agent_tool(
        name="list_spending_transactions",
        args=args,
        user_id=USER,
        workspace_id=WS,
        user_timezone=tz,
    )


async def _find(tz=TZ, **args):
    return await execute_agent_tool(
        name="find_spending_transactions",
        args=args,
        user_id=USER,
        workspace_id=WS,
        user_timezone=tz,
    )


@pytest.mark.asyncio
async def test_list_range_is_inclusive_of_both_local_days_in_ist(seed):
    # IST day boundaries expressed in UTC: 1 Jul 00:00 IST = 30 Jun 18:30Z.
    await _insert_tx(
        seed,
        occurred_at=datetime(2026, 6, 30, 18, 29, 59, 999999, tzinfo=UTC),
        description="before",
    )
    await _insert_tx(
        seed, occurred_at=datetime(2026, 6, 30, 18, 30, tzinfo=UTC), description="first-instant"
    )
    await _insert_tx(
        seed,
        occurred_at=datetime(2026, 7, 3, 18, 29, 59, 999999, tzinfo=UTC),
        description="last-instant",
    )
    await _insert_tx(
        seed, occurred_at=datetime(2026, 7, 3, 18, 30, tzinfo=UTC), description="after"
    )
    res = await _list(from_date="2026-07-01", to_date="2026-07-03", limit=25)
    assert res["status"] == "success"
    assert {t["description"] for t in res["transactions"]} == {"first-instant", "last-instant"}
    assert res["total_count"] == 2
    assert (res["from_date"], res["to_date"]) == ("2026-07-01", "2026-07-03")


@pytest.mark.asyncio
async def test_list_range_bounds_follow_the_users_timezone_not_utc(seed):
    # One instant, two viewers: 2026-07-03 03:00Z is 3 Jul 08:30 IST but 2 Jul 20:00 in LA.
    await _insert_tx(seed, occurred_at=datetime(2026, 7, 3, 3, 0, tzinfo=UTC), description="x")
    in_ist = await _list(from_date="2026-07-03", to_date="2026-07-03")
    in_la = await _list(tz="America/Los_Angeles", from_date="2026-07-03", to_date="2026-07-03")
    la_prev = await _list(tz="America/Los_Angeles", from_date="2026-07-02", to_date="2026-07-02")
    assert in_ist["total_count"] == 1
    assert in_la["total_count"] == 0
    assert la_prev["total_count"] == 1


@pytest.mark.asyncio
async def test_list_day_param_and_default_still_work(seed):
    await _insert_tx(seed, occurred_at=datetime(2026, 7, 3, 6, 0, tzinfo=UTC), description="d")
    res = await _list(day="2026-07-03")
    assert res["total_count"] == 1 and res["day"] == "2026-07-03"
    assert res["next_cursor"] is None
    only_to = await _list(to_date="2026-07-03")  # from defaults to to_date
    assert only_to["total_count"] == 1


@pytest.mark.asyncio
async def test_list_validates_dates(seed):
    assert (await _list(from_date="nope"))["status"] == "error"
    reversed_range = await _list(from_date="2026-07-05", to_date="2026-07-01")
    assert reversed_range["status"] == "error" and "before" in reversed_range["message"]


@pytest.mark.asyncio
async def test_cursor_pagination_walks_every_row_once_in_a_stable_order(seed):
    # 7 rows over 3 days; four share the *same* occurred_at to exercise the tiebreaker.
    tied = datetime(2026, 7, 2, 9, 0, tzinfo=UTC)
    for i in range(4):
        await _insert_tx(seed, occurred_at=tied, description=f"tied-{i}")
    await _insert_tx(seed, occurred_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC), description="d1")
    await _insert_tx(seed, occurred_at=datetime(2026, 7, 3, 9, 0, tzinfo=UTC), description="d3")
    await _insert_tx(seed, occurred_at=datetime(2026, 7, 3, 10, 0, tzinfo=UTC), description="d3b")

    args = {"from_date": "2026-07-01", "to_date": "2026-07-03", "limit": 3}
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = await _list(**args, **({"cursor": cursor} if cursor else {}))
        assert page["status"] == "success", page
        assert page["total_count"] == 7
        seen += [t["entity_public_id"] for t in page["transactions"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert pages < 10
    assert pages == 3
    assert len(seen) == 7 and len(set(seen)) == 7, "no row repeated or skipped"

    # Fetching the whole range in one request yields the identical order.
    whole = await _list(**{**args, "limit": 25})
    assert [t["entity_public_id"] for t in whole["transactions"]] == seen
    assert whole["next_cursor"] is None
    # Newest first.
    stamps = [t["occurred_at"] for t in whole["transactions"]]
    assert stamps == sorted(stamps, reverse=True)

    # Re-reading page 1 gives the same rows (deterministic).
    p1a = await _list(**args)
    p1b = await _list(**args)
    assert [t["entity_public_id"] for t in p1a["transactions"]] == [
        t["entity_public_id"] for t in p1b["transactions"]
    ]


@pytest.mark.asyncio
async def test_cursor_is_bound_to_its_filters_and_rejects_garbage(seed):
    for i in range(3):
        await _insert_tx(seed, occurred_at=datetime(2026, 7, 3, 6 + i, 0, tzinfo=UTC))
    page = await _list(from_date="2026-07-03", to_date="2026-07-03", limit=1)
    assert page["next_cursor"]
    wrong = await _list(
        from_date="2026-07-02", to_date="2026-07-03", limit=1, cursor=page["next_cursor"]
    )
    assert wrong["status"] == "error" and "cursor" in wrong["message"]
    garbage = await _list(from_date="2026-07-03", cursor="zzz")
    assert garbage["status"] == "error"


@pytest.mark.asyncio
async def test_list_and_find_are_workspace_scoped(seed):
    await _insert_tx(seed, occurred_at=datetime(2026, 7, 3, 6, 0, tzinfo=UTC), description="mine")
    await _insert_tx(
        seed,
        occurred_at=datetime(2026, 7, 3, 6, 0, tzinfo=UTC),
        description="theirs",
        workspace=WS + 1,
    )
    listed = await _list(from_date="2026-07-01", to_date="2026-07-31")
    found = await _find(from_date="2026-07-01", to_date="2026-07-31")
    assert listed["total_count"] == 1 and found["total_count"] == 1
    assert [t["description"] for t in listed["transactions"]] == ["mine"]
    assert [t["description"] for t in found["transactions"]] == ["mine"]


@pytest.mark.asyncio
async def test_find_accepts_from_date_to_date_and_paginates_instead_of_erroring(seed):
    for i in range(5):
        await _insert_tx(
            seed, occurred_at=datetime(2026, 7, 3, 6 + i, 0, tzinfo=UTC), description="lunch"
        )
    first = await _find(from_date="2026-07-01", to_date="2026-07-31", search="lunch", limit=2)
    assert first["status"] == "success", first  # used to be a needs_filter error
    assert first["total"] == first["total_count"] == 5
    assert len(first["transactions"]) == 2 and first["next_cursor"]
    second = await _find(
        from_date="2026-07-01",
        to_date="2026-07-31",
        search="lunch",
        limit=2,
        cursor=first["next_cursor"],
    )
    third = await _find(
        from_date="2026-07-01",
        to_date="2026-07-31",
        search="lunch",
        limit=2,
        cursor=second["next_cursor"],
    )
    ids = [t["entity_public_id"] for p in (first, second, third) for t in p["transactions"]]
    assert len(ids) == len(set(ids)) == 5 and third["next_cursor"] is None


@pytest.mark.asyncio
async def test_find_from_date_alone_counts_as_a_clue_and_legacy_names_still_work(seed):
    await _insert_tx(seed, occurred_at=datetime(2026, 7, 3, 6, 0, tzinfo=UTC), description="d")
    by_new = await _find(from_date="2026-07-03")
    by_old = await _find(from_day="2026-07-03", to_day="2026-07-03")
    assert by_new["status"] == by_old["status"] == "success"
    assert by_new["total_count"] == by_old["total"] == 1
    assert (await _find())["needs_filter"] is True  # still demands a clue


@pytest.mark.asyncio
async def test_find_range_excludes_the_next_local_midnight(seed):
    # The previous implementation passed the *next* midnight as an inclusive bound.
    await _insert_tx(
        seed, occurred_at=datetime(2026, 7, 3, 18, 30, tzinfo=UTC), description="4 Jul 00:00 IST"
    )
    res = await _find(from_date="2026-07-03", to_date="2026-07-03")
    assert res["total_count"] == 0


# ------------------------------------------------------------- MCP pass-through


@pytest.mark.asyncio
async def test_mcp_wrappers_forward_new_parameters(monkeypatch):
    calls: list[tuple] = []

    async def fake_run(workspace_id, required_scope, tool_name, kwargs):
        calls.append((workspace_id, required_scope, tool_name, kwargs))
        return {"status": "success"}

    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.lifestack.test")
    monkeypatch.setattr("app.mcp.support.common.run_capture_tool", fake_run)
    tools = {t.name: t.fn for t in await create_mcp_server().list_tools()}

    await tools["log_spending_transactions_batch"](
        workspace_id=7, items=[{"amount": "1"}], atomic=True, dry_run=True, skip_duplicates=True
    )
    await tools["log_spending_transaction"](
        workspace_id=7, amount="1", category_name="food", dry_run=True, skip_duplicates=True
    )
    await tools["list_spending_transactions"](
        workspace_id=7, from_date="2026-07-01", to_date="2026-07-03", cursor="abc"
    )
    await tools["find_spending_transactions"](
        workspace_id=7, from_date="2026-07-01", to_date="2026-07-03", cursor="abc"
    )
    batch, single, listed, found = calls
    assert batch[1:3] == ("mcp:write", "log_spending_transactions_batch")
    assert batch[3] == {
        "items": [{"amount": "1"}],
        "atomic": True,
        "dry_run": True,
        "skip_duplicates": True,
    }
    assert single[3]["dry_run"] is True and single[3]["skip_duplicates"] is True
    assert listed[1] == found[1] == "mcp:read"
    for call in (listed, found):
        assert call[3]["from_date"] == "2026-07-01"
        assert call[3]["to_date"] == "2026-07-03"
        assert call[3]["cursor"] == "abc"


@pytest.mark.asyncio
async def test_mcp_batch_runs_through_the_real_runner_with_mcp_agent_source(seed, monkeypatch):
    async def authorize(*args, **kwargs):
        return USER

    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.lifestack.test")
    monkeypatch.setattr("app.mcp.security.authorize_workspace", authorize)
    tools = {t.name: t.fn for t in await create_mcp_server().list_tools()}
    res = await tools["log_spending_transactions_batch"](
        workspace_id=WS, items=[_chai(source_ref="mcp-1")]
    )
    assert res["status"] == "success" and res["counts"]["create"] == 1
    (row,) = await _rows()
    assert row.source_type == TransactionSourceType.mcp_agent and row.source_ref == "mcp-1"
    again = await tools["log_spending_transactions_batch"](
        workspace_id=WS, items=[_chai(source_ref="mcp-1")]
    )
    assert again["items"][0]["status"] == "duplicate"
    assert len(await _rows()) == 1


def test_decimal_money_is_never_float():
    # Guard against regressions to float arithmetic in the batch path.
    source = inspect.getsource(spending_batch)
    assert "float(" not in source
