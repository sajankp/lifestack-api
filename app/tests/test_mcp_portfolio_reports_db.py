"""DB-backed tests for the portfolio report MCP tools (spec-107).

Real Postgres (testcontainers) through the tool functions themselves, with only
``authorize_workspace`` patched, so the tests exercise the actual repository
SQL, the append-only ON CONFLICT behaviour, scoping and pagination.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.auth.models import User
from app.config import settings
from app.core.database import postgres
from app.core.exceptions import ForbiddenError
from app.investing.models import PortfolioReport
from app.mcp.server import MAX_PORTFOLIO_REPORT_CHARS, create_mcp_server
from app.platform.models import Workspace, WorkspaceMembership

WS = 8201
OTHER_WS = 8202
ALICE = 8201
BOB = 8202


@pytest.fixture(autouse=True)
def _mcp_settings(monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.lifestack.test")


@pytest.fixture
def authorize(monkeypatch):
    mock = AsyncMock(return_value=ALICE)
    monkeypatch.setattr("app.mcp.security.authorize_workspace", mock)
    return mock


async def _tools():
    server = create_mcp_server()
    return {t.name: t.fn for t in await server.list_tools()}


async def _seed_workspaces() -> None:
    async with postgres.async_session_maker() as session, session.begin():
        session.add_all([
            User(
                id=ALICE,
                email="alice-reports@example.com",
                username="alice_reports",
                hashed_password="x",
            ),
            User(
                id=BOB, email="bob-reports@example.com", username="bob_reports", hashed_password="x"
            ),
            Workspace(id=WS, name="Reports"),
            Workspace(id=OTHER_WS, name="Other"),
        ])
        await session.flush()
        session.add_all([
            WorkspaceMembership(workspace_id=WS, user_id=ALICE, role="owner"),
            WorkspaceMembership(workspace_id=OTHER_WS, user_id=BOB, role="owner"),
        ])


def _report(**overrides):
    base = {
        "workspace_id": WS,
        "as_of": "2026-11-07",
        "run_id": "2026-11-07_abc12345",
        "scalars_json": {"equity_inr": "120000.00"},
        "foundation_json": {"themes": ["semis_ai"]},
        "actions_md": "# Actions\n- hold",
        "theme_weights": {"semis_ai": "0.25"},
        "harvest_candidates": [{"symbol": "ABC", "loss_inr": "1500.00"}],
        "lrs_headroom_inr": "45000.50",
        "tax_buckets": {"lt_gain_inr": "100.00", "total_tax_inr": "12.50"},
    }
    base.update(overrides)
    return base


async def _row_count(workspace_id: int = WS) -> int:
    async with postgres.async_session_maker() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(PortfolioReport)
                .where(PortfolioReport.workspace_id == workspace_id)
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_create_then_list_round_trips_every_column(override_database_url, authorize):
    await _seed_workspaces()
    tools = await _tools()

    created = await tools["create_portfolio_report"](**_report())
    assert created["status"] == "success"
    assert created["run_id"] == "2026-11-07_abc12345"
    assert created["as_of"] == "2026-11-07"

    listed = await tools["list_portfolio_reports"](workspace_id=WS, include_payload=True)
    assert listed["status"] == "success"
    assert listed["total"] == 1
    assert listed["next_cursor"] is None
    item = listed["items"][0]
    assert item["public_id"] == created["public_id"]
    assert item["theme_weights"] == {"semis_ai": "0.25"}
    assert item["harvest_candidates"] == [{"symbol": "ABC", "loss_inr": "1500.00"}]
    assert item["lrs_headroom_inr"] == "45000.50"
    assert item["tax_buckets"]["total_tax_inr"] == "12.50"
    assert item["foundation_json"] == {"themes": ["semis_ai"]}
    assert item["actions_md"] == "# Actions\n- hold"
    assert item["scalars_json"] == {"equity_inr": "120000.00"}


@pytest.mark.asyncio
async def test_create_is_append_only_same_run_id_never_overwrites(override_database_url, authorize):
    await _seed_workspaces()
    tools = await _tools()

    first = await tools["create_portfolio_report"](**_report())
    again = await tools["create_portfolio_report"](
        **_report(actions_md="CHANGED", lrs_headroom_inr="1.00", theme_weights={"x": "1"})
    )

    assert again["status"] == "success"
    assert again["public_id"] == first["public_id"]
    assert await _row_count() == 1
    stored = (await tools["list_portfolio_reports"](workspace_id=WS, include_payload=True))[
        "items"
    ][0]
    assert stored["actions_md"] == "# Actions\n- hold"
    assert stored["lrs_headroom_inr"] == "45000.50"
    assert stored["theme_weights"] == {"semis_ai": "0.25"}


@pytest.mark.asyncio
async def test_new_run_id_same_day_adds_a_row(override_database_url, authorize):
    await _seed_workspaces()
    tools = await _tools()

    a = await tools["create_portfolio_report"](**_report(run_id="run-a"))
    b = await tools["create_portfolio_report"](**_report(run_id="run-b"))

    assert a["public_id"] != b["public_id"]
    assert await _row_count() == 2


@pytest.mark.asyncio
async def test_list_filters_range_orders_newest_first_and_paginates_completely(
    override_database_url, authorize
):
    await _seed_workspaces()
    tools = await _tools()
    for day in ("2026-10-10", "2026-10-24", "2026-11-07", "2026-11-21"):
        await tools["create_portfolio_report"](**_report(as_of=day, run_id=f"r-{day}"))

    full = await tools["list_portfolio_reports"](workspace_id=WS)
    assert [i["as_of"] for i in full["items"]] == [
        "2026-11-21",
        "2026-11-07",
        "2026-10-24",
        "2026-10-10",
    ]

    # inclusive on both ends
    ranged = await tools["list_portfolio_reports"](
        workspace_id=WS, from_date="2026-10-24", to_date="2026-11-07"
    )
    assert ranged["total"] == 2
    assert {i["as_of"] for i in ranged["items"]} == {"2026-10-24", "2026-11-07"}

    # paging walks every row exactly once and then stops
    seen, cursor, pages = [], None, 0
    while True:
        page = await tools["list_portfolio_reports"](workspace_id=WS, limit=3, cursor=cursor)
        seen += [i["public_id"] for i in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 2
    assert len(seen) == len(set(seen)) == 4


@pytest.mark.asyncio
async def test_reports_are_scoped_per_workspace(override_database_url, authorize):
    await _seed_workspaces()
    tools = await _tools()
    await tools["create_portfolio_report"](**_report())
    await tools["create_portfolio_report"](**_report(workspace_id=OTHER_WS))

    mine = await tools["list_portfolio_reports"](workspace_id=WS)
    theirs = await tools["list_portfolio_reports"](workspace_id=OTHER_WS)

    assert mine["total"] == 1 and theirs["total"] == 1
    assert mine["items"][0]["public_id"] != theirs["items"][0]["public_id"]
    # same (as_of, run_id) is allowed in two different workspaces
    assert await _row_count(WS) == 1 and await _row_count(OTHER_WS) == 1


@pytest.mark.asyncio
async def test_scopes_requested_are_write_for_create_and_read_for_list(
    override_database_url, authorize
):
    await _seed_workspaces()
    tools = await _tools()

    await tools["create_portfolio_report"](**_report())
    assert authorize.await_args.kwargs["required_scope"] == "mcp:write"
    assert authorize.await_args.kwargs["tool"] == "create_portfolio_report"

    await tools["list_portfolio_reports"](workspace_id=WS)
    assert authorize.await_args.kwargs["required_scope"] == "mcp:read"
    assert authorize.await_args.kwargs["tool"] == "list_portfolio_reports"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"as_of": "07/11/2026"}, "as_of"),
        ({"run_id": ""}, "run_id"),
        ({"run_id": "x" * 129}, "run_id"),
        ({"lrs_headroom_inr": "lots"}, "lrs_headroom_inr"),
    ],
)
async def test_create_rejects_bad_input_without_writing(
    override_database_url, authorize, overrides, fragment
):
    await _seed_workspaces()
    tools = await _tools()

    res = await tools["create_portfolio_report"](**_report(**overrides))

    assert res["status"] == "error"
    assert fragment in res["message"]
    assert await _row_count() == 0


@pytest.mark.asyncio
async def test_create_rejects_oversized_payload(override_database_url, authorize):
    await _seed_workspaces()
    tools = await _tools()

    res = await tools["create_portfolio_report"](
        **_report(actions_md="x" * (MAX_PORTFOLIO_REPORT_CHARS + 1))
    )

    assert res["status"] == "error"
    assert "exceeds" in res["message"]
    assert await _row_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 101},
        {"cursor": -1},
        {"from_date": "yesterday"},
        {"to_date": "2026-13-45"},
    ],
)
async def test_list_validates_inputs(override_database_url, authorize, kwargs):
    await _seed_workspaces()
    tools = await _tools()

    res = await tools["list_portfolio_reports"](workspace_id=WS, **kwargs)

    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_lrs_headroom_keeps_decimal_precision(override_database_url, authorize):
    await _seed_workspaces()
    tools = await _tools()
    await tools["create_portfolio_report"](**_report(lrs_headroom_inr="12345678.91"))

    async with postgres.async_session_maker() as session:
        row = (await session.execute(select(PortfolioReport))).scalars().one()
    assert row.lrs_headroom_inr == Decimal("12345678.91")
    assert row.as_of == date(2026, 11, 7)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("create_portfolio_report", {"as_of": "2026-11-07", "run_id": "r"}),
        ("list_portfolio_reports", {}),
    ],
)
async def test_both_tools_stop_when_authorization_fails(
    override_database_url, monkeypatch, tool, kwargs
):
    monkeypatch.setattr(
        "app.mcp.security.authorize_workspace",
        AsyncMock(side_effect=ForbiddenError(detail="no access")),
    )
    await _seed_workspaces()
    tools = await _tools()

    with pytest.raises(ForbiddenError):
        await tools[tool](workspace_id=WS, **kwargs)
    assert await _row_count() == 0


@pytest.mark.asyncio
async def test_resend_reports_whether_the_run_was_created_and_whether_content_matched(
    override_database_url, authorize
):
    await _seed_workspaces()
    tools = await _tools()

    first = await tools["create_portfolio_report"](**_report())
    assert first["created"] is True and first["content_matches"] is True
    assert "warning" not in first

    same = await tools["create_portfolio_report"](**_report())
    assert same["created"] is False and same["content_matches"] is True
    assert same["public_id"] == first["public_id"] and "warning" not in same

    changed = await tools["create_portfolio_report"](**_report(actions_md="CORRECTED"))
    assert changed["status"] == "success"
    assert changed["created"] is False and changed["content_matches"] is False
    assert changed["public_id"] == first["public_id"]
    assert "new run_id" in changed["warning"], "a dropped correction must be visible to the caller"


@pytest.mark.asyncio
async def test_omitted_fields_are_stored_as_empty_containers_not_json_null(
    override_database_url, authorize
):
    await _seed_workspaces()
    tools = await _tools()

    await tools["create_portfolio_report"](workspace_id=WS, as_of="2026-11-07", run_id="bare")

    item = (await tools["list_portfolio_reports"](workspace_id=WS, include_payload=True))["items"][
        0
    ]
    assert item["scalars_json"] == {} and item["foundation_json"] == {}
    assert item["theme_weights"] == {} and item["tax_buckets"] == {}
    assert item["harvest_candidates"] == []
    assert item["lrs_headroom_inr"] is None and item["actions_md"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "1e20", "10000000000000000"])
async def test_non_finite_or_overflowing_headroom_is_rejected_cleanly(
    override_database_url, authorize, bad
):
    await _seed_workspaces()
    tools = await _tools()

    res = await tools["create_portfolio_report"](**_report(lrs_headroom_inr=bad))

    assert res["status"] == "error" and "lrs_headroom_inr" in res["message"], (bad, res)
    assert await _row_count() == 0


@pytest.mark.asyncio
async def test_headroom_is_rounded_to_the_column_scale(override_database_url, authorize):
    await _seed_workspaces()
    tools = await _tools()
    await tools["create_portfolio_report"](**_report(lrs_headroom_inr="12.345"))

    item = (await tools["list_portfolio_reports"](workspace_id=WS))["items"][0]
    assert item["lrs_headroom_inr"] == "12.35"


@pytest.mark.asyncio
async def test_tax_buckets_count_towards_the_payload_cap(override_database_url, authorize):
    await _seed_workspaces()
    tools = await _tools()

    res = await tools["create_portfolio_report"](
        **_report(tax_buckets={"note": "x" * (MAX_PORTFOLIO_REPORT_CHARS + 1)})
    )

    assert res["status"] == "error" and "exceeds" in res["message"]
    assert await _row_count() == 0


@pytest.mark.asyncio
async def test_list_returns_summaries_by_default_and_the_payload_on_request(
    override_database_url, authorize
):
    await _seed_workspaces()
    tools = await _tools()
    await tools["create_portfolio_report"](**_report())

    summary = (await tools["list_portfolio_reports"](workspace_id=WS))["items"][0]
    full = (await tools["list_portfolio_reports"](workspace_id=WS, include_payload=True))["items"][
        0
    ]

    for heavy in ("foundation_json", "actions_md", "scalars_json"):
        assert heavy not in summary, f"{heavy} must be opt-in"
        assert heavy in full
    assert summary["theme_weights"] == {"semis_ai": "0.25"}
    assert summary["lrs_headroom_inr"] == "45000.50"
