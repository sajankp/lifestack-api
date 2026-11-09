"""Backfill of historical Rule 115 FX rates over MCP (``set_fx_rates``).

A financial year can have hundreds of missing daily rates (``list_missing_fx_rates``), so
setting them one call at a time is impractical. The bulk tool validates everything before
writing anything, never overwrites a rate the user already set unless asked, previews
before it writes, and merges under a row lock so concurrent writers don't lose entries.
"""

import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.models import User
from app.config import settings
from app.core.database import postgres
from app.core.exceptions import ForbiddenError
from app.finance.models import WorkspaceTaxProfile
from app.mcp.server import create_mcp_server
from app.platform.models import Workspace, WorkspaceMembership

WS = 8401
OTHER_WS = 8402
ALICE = 8401
BOB = 8402


@pytest.fixture(autouse=True)
def _mcp_settings(monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.lifestack.test")


@pytest.fixture
def authorize(monkeypatch):
    mock = AsyncMock(return_value=ALICE)
    monkeypatch.setattr("app.mcp.security.authorize_workspace", mock)
    return mock


async def _tools():
    return {t.name: t.fn for t in await create_mcp_server().list_tools()}


async def _seed() -> None:
    async with postgres.async_session_maker() as session, session.begin():
        session.add_all([
            User(id=ALICE, email="a-fx@example.com", username="a_fx", hashed_password="x"),
            User(id=BOB, email="b-fx@example.com", username="b_fx", hashed_password="x"),
            Workspace(id=WS, name="FX"),
            Workspace(id=OTHER_WS, name="Other FX"),
        ])
        await session.flush()
        session.add_all([
            WorkspaceMembership(workspace_id=WS, user_id=ALICE, role="owner"),
            WorkspaceMembership(workspace_id=OTHER_WS, user_id=BOB, role="owner"),
        ])


async def _stored(workspace_id: int = WS) -> dict:
    async with postgres.async_session_maker() as session:
        row = (
            await session.execute(
                select(WorkspaceTaxProfile).where(WorkspaceTaxProfile.workspace_id == workspace_id)
            )
        ).scalar_one_or_none()
    return dict(row.fx_overrides or {}) if row is not None else {}


def _rates(*items):
    return [{"date": d, "currency": c, "rate": r} for d, c, r in items]


@pytest.mark.asyncio
async def test_unconfirmed_call_previews_and_writes_nothing(override_database_url, authorize):
    await _seed()
    tools = await _tools()

    res = await tools["set_fx_rates"](
        workspace_id=WS, rates=_rates(("2026-04-30", "USD", "83.4"), ("2026-05-31", "USD", "84"))
    )

    assert res["status"] == "needs_confirmation" and res["needs_confirmation"] is True
    assert res["counts"] == {"add": 2, "update": 0, "skip_existing": 0, "unchanged": 0}
    assert await _stored() == {}, "a preview must not write"
    assert authorize.await_args.kwargs["required_scope"] == "mcp:write"


@pytest.mark.asyncio
async def test_confirmed_backfill_persists_every_rate_and_the_read_tool_sees_them(
    override_database_url, authorize
):
    await _seed()
    tools = await _tools()
    batch = _rates(
        ("2026-04-30", "USD", "83.4"),
        ("2026-05-31", "usd", "84"),  # currency is case-insensitive
        ("2026-04-30", "EUR", "90.25"),
    )

    res = await tools["set_fx_rates"](workspace_id=WS, rates=batch, confirmed=True)

    assert res["status"] == "success", res
    assert res["counts"] == {"added": 3, "updated": 0, "skipped_existing": 0, "unchanged": 0}
    assert await _stored() == {
        "2026-04-30:USD": "83.4",
        "2026-05-31:USD": "84",
        "2026-04-30:EUR": "90.25",
    }
    # Separate session / the real read tool: the writes were committed.
    for d, c, rate in (("2026-04-30", "USD", "83.4"), ("2026-04-30", "EUR", "90.25")):
        read = await tools["get_fx_rate"](workspace_id=WS, date_str=d, currency=c)
        assert read["status"] == "success" and read["rate"] == rate
    missing = await tools["list_missing_fx_rates"](
        workspace_id=WS, from_date="2026-04-29", to_date="2026-05-01", currency="USD"
    )
    assert "2026-04-30" not in repr(missing)


@pytest.mark.asyncio
async def test_existing_rates_are_kept_by_default_and_replaced_only_on_request(
    override_database_url, authorize
):
    await _seed()
    tools = await _tools()
    await tools["set_fx_rates"](
        workspace_id=WS, rates=_rates(("2026-04-30", "USD", "83.4")), confirmed=True
    )

    again = await tools["set_fx_rates"](
        workspace_id=WS,
        rates=_rates(
            ("2026-04-30", "USD", "99"),  # differs -> skipped, not overwritten
            ("2026-05-31", "USD", "84"),  # new
        ),
        confirmed=True,
    )
    assert again["counts"] == {"added": 1, "updated": 0, "skipped_existing": 1, "unchanged": 0}
    assert again["skipped_existing"] == ["2026-04-30:USD"]
    assert (await _stored())["2026-04-30:USD"] == "83.4"

    same = await tools["set_fx_rates"](
        workspace_id=WS, rates=_rates(("2026-04-30", "USD", "83.40")), confirmed=True
    )
    assert same["counts"]["unchanged"] == 1 and same["counts"]["skipped_existing"] == 0

    forced = await tools["set_fx_rates"](
        workspace_id=WS,
        rates=_rates(("2026-04-30", "USD", "99")),
        overwrite_existing=True,
        confirmed=True,
    )
    assert forced["counts"]["updated"] == 1
    assert (await _stored())["2026-04-30:USD"] == "99"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad", "fragment"),
    [
        (("2026-13-45", "USD", "83"), "date"),
        (("30-04-2026", "USD", "83"), "date"),
        (("2999-01-01", "USD", "83"), "future"),
        (("2026-04-30", "US", "83"), "currency"),
        (("2026-04-30", "USD", "abc"), "rate"),
        (("2026-04-30", "USD", "NaN"), "rate"),
        (("2026-04-30", "USD", "0"), "rate"),
        (("2026-04-30", "USD", "-5"), "rate"),
        (("2026-04-30", "USD", "1001"), "rate"),
    ],
)
async def test_one_bad_entry_rejects_the_whole_batch_and_names_it(
    override_database_url, authorize, bad, fragment
):
    await _seed()
    tools = await _tools()
    batch = _rates(("2026-03-31", "USD", "82"), bad, ("2026-05-31", "USD", "84"))

    res = await tools["set_fx_rates"](workspace_id=WS, rates=batch, confirmed=True)

    assert res["status"] == "error", res
    assert "rates[1]" in res["message"] and fragment in res["message"].lower(), res["message"]
    assert await _stored() == {}, "nothing is written when any entry is invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rates",
    [
        [],
        "not a list",
        [{"date": "2026-04-30", "currency": "USD"}],  # missing rate
        [{"date": "2026-04-30", "currency": "USD", "rate": "83", "extra": 1}],  # typo-guard
        [
            {"date": "2026-04-30", "currency": "USD", "rate": "83"},
            {"date": "2026-04-30", "currency": "usd", "rate": "84"},  # same key twice
        ],
        [
            {"date": f"2026-01-{(i % 28) + 1:02d}", "currency": "USD", "rate": "83"}
            for i in range(401)
        ],
    ],
    ids=["empty", "not-a-list", "missing-field", "unknown-field", "duplicate-key", "over-400"],
)
async def test_malformed_batches_are_rejected_without_writing(
    override_database_url, authorize, rates
):
    await _seed()
    tools = await _tools()

    res = await tools["set_fx_rates"](workspace_id=WS, rates=rates, confirmed=True)

    assert res["status"] == "error", res
    assert await _stored() == {}


@pytest.mark.asyncio
async def test_a_full_year_of_daily_rates_fits_in_one_call(override_database_url, authorize):
    await _seed()
    tools = await _tools()
    start = date(2023, 4, 1)  # in the past so no date is "in the future"
    batch = [
        {"date": (start + timedelta(days=i)).isoformat(), "currency": "USD", "rate": "83.5"}
        for i in range(366)
    ]

    res = await tools["set_fx_rates"](workspace_id=WS, rates=batch, confirmed=True)

    assert res["status"] == "success" and res["counts"]["added"] == 366, res
    assert len(await _stored()) == 366


@pytest.mark.asyncio
async def test_rates_are_scoped_to_their_workspace(override_database_url, authorize):
    await _seed()
    tools = await _tools()

    await tools["set_fx_rates"](
        workspace_id=WS, rates=_rates(("2026-04-30", "USD", "83.4")), confirmed=True
    )

    assert await _stored(WS) == {"2026-04-30:USD": "83.4"}
    assert await _stored(OTHER_WS) == {}


@pytest.mark.asyncio
async def test_concurrent_backfills_do_not_lose_each_others_entries(race_engine, monkeypatch):
    """Two writers on real, separate connections: the row lock must serialise the merge.

    ``fx_overrides`` is one JSON document; without ``SELECT ... FOR UPDATE`` both writers
    read the old document and each saves only its own entries (a lost update). This needs
    genuinely independent committing connections, so it uses ``race_engine`` rather than the
    single-connection savepoint fixture, where concurrent sessions cannot work.
    """
    maker = async_sessionmaker(race_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session, session.begin():
        user = User(email="race-fx@example.com", username="race_fx", hashed_password="x")
        workspace = Workspace(name="race-fx")
        session.add_all([user, workspace])
        await session.flush()
        session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="owner"))
        user_id, workspace_id = user.id, workspace.id
    monkeypatch.setattr(postgres, "async_session_maker", maker)
    monkeypatch.setattr("app.mcp.security.authorize_workspace", AsyncMock(return_value=user_id))
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.lifestack.test")
    tools = await _tools()
    # The profile row exists first, so this exercises the merge race (not first-row creation).
    seeded = await tools["set_fx_rates"](
        workspace_id=workspace_id, rates=_rates(("2026-03-31", "USD", "82")), confirmed=True
    )
    assert seeded["status"] == "success", seeded

    a = _rates(*[(f"2026-04-{d:02d}", "USD", "83") for d in range(1, 16)])
    b = _rates(*[(f"2026-04-{d:02d}", "EUR", "90") for d in range(1, 16)])
    ra, rb = await asyncio.gather(
        tools["set_fx_rates"](workspace_id=workspace_id, rates=a, confirmed=True),
        tools["set_fx_rates"](workspace_id=workspace_id, rates=b, confirmed=True),
    )

    assert ra["status"] == rb["status"] == "success", (ra, rb)
    async with maker() as session:
        row = (
            await session.execute(
                select(WorkspaceTaxProfile).where(WorkspaceTaxProfile.workspace_id == workspace_id)
            )
        ).scalar_one()
    assert len(row.fx_overrides) == 31, "both writers' entries (and the seed) must survive"


@pytest.mark.asyncio
async def test_single_set_fx_rate_shares_the_locked_merge_and_still_overwrites(
    override_database_url, authorize
):
    await _seed()
    tools = await _tools()
    await tools["set_fx_rates"](
        workspace_id=WS, rates=_rates(("2026-04-30", "USD", "83.4")), confirmed=True
    )

    res = await tools["set_fx_rate"](
        workspace_id=WS, date_str="2026-04-30", currency="USD", rate="84.1", confirmed=True
    )

    assert res["status"] == "success"
    assert (await _stored())["2026-04-30:USD"] == "84.1", (
        "the single tool is a deliberate correction"
    )


@pytest.mark.asyncio
async def test_authorization_failure_writes_nothing(override_database_url, monkeypatch):
    monkeypatch.setattr(
        "app.mcp.security.authorize_workspace",
        AsyncMock(side_effect=ForbiddenError(detail="no access")),
    )
    await _seed()
    tools = await _tools()

    with pytest.raises(ForbiddenError):
        await tools["set_fx_rates"](
            workspace_id=WS, rates=_rates(("2026-04-30", "USD", "83.4")), confirmed=True
        )
    assert await _stored() == {}
