"""API tests for the portfolio report history routes.

GET /v1/investing/portfolio-reports   list, cursor-paginated, newest ``as_of`` first
GET /v1/investing/portfolio-reports/{public_id}   detail
"""

from datetime import date
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.auth.models import User
from app.core.database import postgres
from app.investing.models import PortfolioReport
from app.platform.models import Workspace, WorkspaceMembership

LIST_PATH = "/v1/investing/portfolio-reports"

BASE_ROW = {
    "theme_weights": {"equity": "0.60", "debt": "0.40"},
    "harvest_candidates": [{"symbol": "ABC", "loss_inr": "1500.00"}],
    "lrs_headroom_inr": "45000.50",
    "tax_buckets": {"lt_gain_inr": "100.00", "total_tax_inr": "12.50"},
}

PAYLOAD_ROW = {
    "scalars_json": {"equity_inr": "120000.00"},
    "foundation_json": {"themes": ["semis_ai"]},
    "actions_md": "# Actions\n- hold",
}

DENORMALIZED_KEYS = {
    "public_id",
    "as_of",
    "run_id",
    "theme_weights",
    "harvest_candidates",
    "lrs_headroom_inr",
    "tax_buckets",
    "created_at",
}

PAYLOAD_KEYS = {"scalars_json", "foundation_json", "actions_md"}


async def _register_and_login(client: AsyncClient) -> tuple[int, int]:
    credentials = {
        "email": "reports@example.com",
        "username": "reports_user",
        "password": "TestPass123!",
    }
    register_res = await client.post("/v1/auth/register", json=credentials)
    assert register_res.status_code == 200, register_res.text
    login_res = await client.post(
        "/v1/auth/login",
        data={"username": credentials["username"], "password": credentials["password"]},
    )
    assert login_res.status_code == 200, login_res.text
    async with postgres.async_session_maker() as session:
        user = (
            await session.execute(select(User).where(User.email == credentials["email"]))
        ).scalar_one()
        membership = (
            await session.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)
            )
        ).scalar_one()
    return user.id, membership.workspace_id


async def _seed(session, workspace_id: int, as_of: date, run_id: str, **extra) -> str:
    report = PortfolioReport(
        workspace_id=workspace_id,
        as_of=as_of,
        run_id=run_id,
        **BASE_ROW,
        **extra,
    )
    session.add(report)
    await session.flush()
    await session.commit()
    return str(report.public_id)


@pytest.mark.asyncio
async def test_list_portfolio_reports_newest_first_with_full_page(client: AsyncClient):
    _user_id, workspace_id = await _register_and_login(client)
    async with postgres.async_session_maker() as session:
        await _seed(session, workspace_id, date(2026, 10, 1), "2026-10-01_a")
        await _seed(session, workspace_id, date(2026, 11, 1), "2026-11-01_a")
        await _seed(session, workspace_id, date(2026, 11, 15), "2026-11-15_a")

    res = await client.get(LIST_PATH)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 3
    assert body.get("next_cursor") is None
    assert len(body["items"]) == 3
    assert [item["as_of"] for item in body["items"]] == [
        "2026-11-15",
        "2026-11-01",
        "2026-10-01",
    ]
    for item in body["items"]:
        assert set(item) == DENORMALIZED_KEYS
        assert not PAYLOAD_KEYS.intersection(item)


@pytest.mark.asyncio
async def test_list_portfolio_reports_cursor_pagination(client: AsyncClient):
    _user_id, workspace_id = await _register_and_login(client)
    async with postgres.async_session_maker() as session:
        await _seed(session, workspace_id, date(2026, 10, 1), "2026-10-01_a")
        await _seed(session, workspace_id, date(2026, 11, 1), "2026-11-01_a")
        await _seed(session, workspace_id, date(2026, 11, 15), "2026-11-15_a")

    first = await client.get(LIST_PATH, params={"limit": 1, "cursor": 0})
    assert first.status_code == 200
    body = first.json()
    assert body["total"] == 3
    assert body["next_cursor"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["as_of"] == "2026-11-15"

    second = await client.get(LIST_PATH, params={"limit": 1, "cursor": 1})
    assert second.status_code == 200
    body = second.json()
    assert body["next_cursor"] == 2
    assert body["items"][0]["as_of"] == "2026-11-01"

    third = await client.get(LIST_PATH, params={"limit": 1, "cursor": 2})
    assert third.status_code == 200
    body = third.json()
    assert body.get("next_cursor") is None
    assert body["items"][0]["as_of"] == "2026-10-01"


@pytest.mark.asyncio
async def test_list_portfolio_reports_date_range(client: AsyncClient):
    _user_id, workspace_id = await _register_and_login(client)
    async with postgres.async_session_maker() as session:
        await _seed(session, workspace_id, date(2026, 10, 1), "2026-10-01_a")
        await _seed(session, workspace_id, date(2026, 11, 1), "2026-11-01_a")
        await _seed(session, workspace_id, date(2026, 11, 15), "2026-11-15_a")

    res = await client.get(LIST_PATH, params={"from_date": "2026-11-01"})
    assert res.status_code == 200
    assert res.json()["total"] == 2
    assert [item["as_of"] for item in res.json()["items"]] == ["2026-11-15", "2026-11-01"]

    res = await client.get(LIST_PATH, params={"to_date": "2026-10-31"})
    assert res.status_code == 200
    assert res.json()["total"] == 1
    assert res.json()["items"][0]["as_of"] == "2026-10-01"

    res = await client.get(
        LIST_PATH, params={"from_date": "2026-11-01", "to_date": "2026-11-10"}
    )
    assert res.status_code == 200
    assert res.json()["total"] == 1
    assert res.json()["items"][0]["as_of"] == "2026-11-01"


@pytest.mark.asyncio
async def test_list_portfolio_reports_include_payload(client: AsyncClient):
    _user_id, workspace_id = await _register_and_login(client)
    async with postgres.async_session_maker() as session:
        public_id = await _seed(
            session, workspace_id, date(2026, 11, 1), "2026-11-01_full", **PAYLOAD_ROW
        )

    res = await client.get(LIST_PATH)
    assert res.status_code == 200
    assert not PAYLOAD_KEYS.intersection(res.json()["items"][0])

    res = await client.get(LIST_PATH, params={"include_payload": "true"})
    assert res.status_code == 200
    item = next(i for i in res.json()["items"] if i["public_id"] == public_id)
    assert item["scalars_json"] == PAYLOAD_ROW["scalars_json"]
    assert item["foundation_json"] == PAYLOAD_ROW["foundation_json"]
    assert item["actions_md"] == PAYLOAD_ROW["actions_md"]


@pytest.mark.asyncio
async def test_get_portfolio_report_by_public_id(client: AsyncClient):
    _user_id, workspace_id = await _register_and_login(client)
    async with postgres.async_session_maker() as session:
        public_id = await _seed(session, workspace_id, date(2026, 11, 1), "2026-11-01_a")

    res = await client.get(f"{LIST_PATH}/{public_id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["public_id"] == public_id
    assert body["as_of"] == "2026-11-01"
    assert body["run_id"] == "2026-11-01_a"
    assert set(body) == DENORMALIZED_KEYS

    res = await client.get(f"{LIST_PATH}/{public_id}", params={"include_payload": "true"})
    assert res.status_code == 200
    body = res.json()
    assert body.get("scalars_json") == {}
    assert body.get("foundation_json") == {}
    assert body.get("actions_md") is None

    missing = await client.get(f"{LIST_PATH}/{uuid4()}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_list_portfolio_reports_excludes_other_workspace(client: AsyncClient):
    _user_id, workspace_id = await _register_and_login(client)
    async with postgres.async_session_maker() as session:
        await _seed(session, workspace_id, date(2026, 11, 1), "2026-11-01_alice")
        other_ws = Workspace(name="Other workspace")
        session.add(other_ws)
        await session.flush()
        other_ws_id = other_ws.id
        assert other_ws_id is not None
        session.add(
            WorkspaceMembership(workspace_id=other_ws_id, user_id=_user_id, role="owner")
        )
        await _seed(session, other_ws_id, date(2026, 11, 1), "2026-11-01_bob")

    res = await client.get(LIST_PATH)
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["run_id"] == "2026-11-01_alice"


@pytest.mark.asyncio
async def test_list_portfolio_reports_input_validation(client: AsyncClient):
    await _register_and_login(client)
    for params in [
        {"limit": 0},
        {"limit": 101},
        {"cursor": -1},
        {"from_date": "not-a-date"},
    ]:
        res = await client.get(LIST_PATH, params=params)
        assert res.status_code == 422, f"{params}: {res.text}"
