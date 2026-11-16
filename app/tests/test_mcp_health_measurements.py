"""MCP health measurement tools against real Postgres (spec-111).

Only ``authorize_workspace`` is patched, so the tools' real queries, commits and error
shapes run. Every write is read back through a fresh session.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.auth.models import User
from app.config import settings
from app.core.database import postgres
from app.health.measurements.models import HealthMeasurement, HealthRecord
from app.mcp.server import create_mcp_server
from app.platform.models import Workspace

WS = 9401
OTHER_WS = 9402
USER = 9401


@pytest.fixture(autouse=True)
def _mcp_settings(monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://api.lifestack.test")


@pytest.fixture
def authorize(monkeypatch):
    mock = AsyncMock(return_value=USER)
    monkeypatch.setattr("app.mcp.security.authorize_workspace", mock)
    return mock


async def _tools():
    server = create_mcp_server()
    return {t.name: t.fn for t in await server.list_tools()}


async def _seed(session) -> None:
    session.add_all([
        User(id=USER, email="mcp-hm@example.com", username="mcp_hm", hashed_password="x"),
        Workspace(id=WS, name="HM"),
        Workspace(id=OTHER_WS, name="Other"),
    ])
    await session.flush()


async def _fresh_count(model) -> int:
    async with postgres.async_session_maker() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


LDL = {
    "name": "LDL Cholesterol",
    "code": "ldl_cholesterol",
    "category": "lipid",
    "canonical_unit": "mg/dL",
    "unit_conversions": {"mmol/L": {"factor": "38.67", "offset": "0"}},
    "aliases": ["LDL-C"],
}
RECORD = {
    "kind": "lab_report",
    "title": "Lipid panel",
    "recorded_on": "2026-11-10",
    "provider": "Metropolis",
    "note": "Extracted from the PDF the lab emailed on 2026-11-11",
    "source_ref": "metropolis-777",
}


@pytest.mark.asyncio
async def test_define_write_preview_confirm_and_read_back(override_database_url, authorize):
    async with postgres.async_session_maker() as session:
        await _seed(session)
        await session.commit()
    tools = await _tools()

    defined = await tools["define_health_metric"](workspace_id=WS, **LDL)
    assert defined["status"] == "success" and defined["outcome"] == "created"
    assert defined["metric"]["code"] == "ldl_cholesterol"
    assert authorize.await_args.kwargs["required_scope"] == "mcp:write"

    items = [{"metric": "LDL-C", "value": "2.5", "unit": "mmol/L", "ref_high": "3.4"}]
    preview = await tools["record_health_results"](workspace_id=WS, items=items, record=RECORD)
    assert preview["dry_run"] is True and "Preview only" in preview["message"]
    assert preview["items"][0]["status"] == "would_create"
    assert await _fresh_count(HealthMeasurement) == 0  # default is a preview: nothing written

    written = await tools["record_health_results"](
        workspace_id=WS, items=items, record=RECORD, confirmed=True
    )
    assert written["status"] == "success" and written["record"]["created"] is True
    assert written["items"][0]["status"] == "created"
    assert authorize.await_args.kwargs["tool"] == "record_health_results"

    # Committed: a fresh session sees the record, its free note, and the row.
    async with postgres.async_session_maker() as session:
        record = (await session.execute(select(HealthRecord))).scalar_one()
        assert record.note == RECORD["note"] and record.source_type == "mcp"
        row = (await session.execute(select(HealthMeasurement))).scalar_one()
        assert (str(row.value_num), row.unit, row.source_type) == ("2.500000", "mmol/L", "mcp")

    again = await tools["record_health_results"](
        workspace_id=WS, items=items, record=RECORD, confirmed=True
    )
    assert again["record"] == {
        "public_id": written["record"]["public_id"],
        "created": False,
        "reused": True,
    }
    assert again["items"][0]["status"] == "duplicate"
    assert await _fresh_count(HealthMeasurement) == 1


@pytest.mark.asyncio
async def test_dry_run_flag_never_writes_even_when_confirmed(override_database_url, authorize):
    async with postgres.async_session_maker() as session:
        await _seed(session)
        await session.commit()
    tools = await _tools()
    await tools["define_health_metric"](workspace_id=WS, **LDL)
    out = await tools["record_health_results"](
        workspace_id=WS,
        items=[{"metric": "LDL-C", "value": "100", "unit": "mg/dL"}],
        record=RECORD,
        confirmed=True,
        dry_run=True,
    )
    assert out["dry_run"] is True and "message" not in out
    assert await _fresh_count(HealthMeasurement) == 0


@pytest.mark.asyncio
async def test_create_missing_metrics_and_series_and_list_tools(override_database_url, authorize):
    async with postgres.async_session_maker() as session:
        await _seed(session)
        await session.commit()
    tools = await _tools()

    out = await tools["record_health_results"](
        workspace_id=WS,
        items=[
            {"metric": "Vitamin D", "value": "28", "unit": "ng/mL", "measured_on": "2026-10-01"},
            {"metric": "Vitamin D", "value": "34", "unit": "ng/mL", "measured_on": "2026-11-01"},
        ],
        record={"kind": "lab_report", "title": "Vit D", "recorded_on": "2026-11-01"},
        create_missing_metrics=True,
        confirmed=True,
    )
    assert out["status"] == "success" and out["counts"]["would_create_metric"] == 1

    metrics = await tools["list_health_metrics"](workspace_id=WS)
    by_code = {m["code"]: m for m in metrics["items"]}
    assert set(by_code) == {"body_weight", "vitamin_d"}
    assert by_code["vitamin_d"]["measurement_count"] == 2
    assert by_code["vitamin_d"]["latest"]["measured_on"] == "2026-11-01"
    assert authorize.await_args.kwargs["required_scope"] == "mcp:read"

    series = await tools["get_health_series"](workspace_id=WS, metric="vitamin d", limit=1)
    assert series["status"] == "success" and series["total"] == 2 and series["has_more"] is True
    assert series["points"][0]["value"] == "34.000000"
    assert series["stats"]["delta_30d"] is None or series["stats"]["latest"] == "34.000000"

    ranged = await tools["get_health_series"](
        workspace_id=WS, metric="vitamin_d", from_date="2026-10-15", to_date="2026-11-30"
    )
    assert ranged["total"] == 1

    records = await tools["list_health_records"](workspace_id=WS)
    assert records["total"] == 1 and records["items"][0]["measurement_count"] == 2
    detail = await tools["list_health_records"](
        workspace_id=WS, record_id=records["items"][0]["public_id"]
    )
    assert [m["metric_code"] for m in detail["record"]["measurements"]] == ["vitamin_d"] * 2


@pytest.mark.asyncio
async def test_errors_are_dicts_not_exceptions(override_database_url, authorize):
    async with postgres.async_session_maker() as session:
        await _seed(session)
        await session.commit()
    tools = await _tools()

    assert (await tools["get_health_series"](workspace_id=WS, metric="nope"))["status"] == "error"
    bad_day = await tools["get_health_series"](workspace_id=WS, metric="body_weight", from_date="x")
    assert bad_day["status"] == "error"
    swapped = await tools["get_health_series"](
        workspace_id=WS, metric="body_weight", from_date="2026-02-01", to_date="2026-01-01"
    )
    assert swapped["status"] == "error"
    assert (await tools["list_health_records"](workspace_id=WS, record_id="zzz"))[
        "status"
    ] == "error"
    assert (await tools["delete_health_record"](workspace_id=WS, public_id="zzz"))[
        "status"
    ] == "error"
    bad_def = await tools["define_health_metric"](workspace_id=WS, name="x", value_type="blob")
    assert bad_def["status"] == "error"
    clash = await tools["define_health_metric"](workspace_id=WS, name="Mass", aliases=["weight"])
    assert clash["status"] == "error" and "body_weight" in clash["message"]
    assert (await tools["record_health_results"](workspace_id=WS, items=[], confirmed=True))[
        "status"
    ] == "error"
    for kwargs in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        with pytest.raises(Exception):  # noqa: B017 - ValidationError from validate_page
            await tools["list_health_records"](workspace_id=WS, **kwargs)


@pytest.mark.asyncio
async def test_delete_tools_require_confirmation_and_cascade(override_database_url, authorize):
    async with postgres.async_session_maker() as session:
        await _seed(session)
        await session.commit()
    tools = await _tools()
    await tools["define_health_metric"](workspace_id=WS, **LDL)
    written = await tools["record_health_results"](
        workspace_id=WS,
        items=[
            {"metric": "LDL-C", "value": "100", "unit": "mg/dL"},
            {"metric": "LDL-C", "value": "110", "unit": "mg/dL", "measured_on": "2026-11-09"},
        ],
        record=RECORD,
        confirmed=True,
    )
    row_id = written["items"][0]["public_id"]
    record_id = written["record"]["public_id"]

    assert (await tools["delete_health_measurement"](workspace_id=WS, public_id=row_id))[
        "deleted"
    ] is False
    assert await _fresh_count(HealthMeasurement) == 2
    gone = await tools["delete_health_measurement"](
        workspace_id=WS, public_id=row_id, confirmed=True
    )
    assert gone["deleted"] is True and await _fresh_count(HealthMeasurement) == 1
    missing = await tools["delete_health_measurement"](
        workspace_id=WS, public_id=row_id, confirmed=True
    )
    assert missing["status"] == "error"

    preview = await tools["delete_health_record"](workspace_id=WS, public_id=record_id)
    assert preview["status"] == "preview" and preview["measurement_count"] == 1
    assert await _fresh_count(HealthRecord) == 1
    done = await tools["delete_health_record"](workspace_id=WS, public_id=record_id, confirmed=True)
    assert done["deleted"] is True
    assert await _fresh_count(HealthRecord) == 0 and await _fresh_count(HealthMeasurement) == 0


@pytest.mark.asyncio
async def test_other_workspaces_data_is_invisible(override_database_url, authorize):
    async with postgres.async_session_maker() as session:
        await _seed(session)
        await session.commit()
    tools = await _tools()
    await tools["define_health_metric"](workspace_id=WS, **LDL)
    written = await tools["record_health_results"](
        workspace_id=WS,
        items=[{"metric": "LDL-C", "value": "100", "unit": "mg/dL"}],
        record=RECORD,
        confirmed=True,
    )
    other = await tools["get_health_series"](workspace_id=OTHER_WS, metric="ldl")
    assert other["status"] == "error"
    assert (await tools["list_health_records"](workspace_id=OTHER_WS))["total"] == 0
    cross = await tools["delete_health_record"](
        workspace_id=OTHER_WS, public_id=written["record"]["public_id"], confirmed=True
    )
    assert cross["status"] == "error"
    assert await _fresh_count(HealthRecord) == 1


@pytest.mark.asyncio
async def test_log_weight_and_history_tools_use_the_metric_store(override_database_url, authorize):
    async with postgres.async_session_maker() as session:
        await _seed(session)
        await session.commit()
    tools = await _tools()

    logged = await tools["log_weight"](workspace_id=WS, weight_kg="72.4", note="morning")
    assert logged["status"] == "success" and logged["weight_kg"] == "72.40"

    history = await tools["get_weight_history"](workspace_id=WS)
    assert [i["weight_kg"] for i in history["items"]] == ["72.40"]
    series = await tools["get_health_series"](workspace_id=WS, metric="body_weight")
    assert series["total"] == 1 and series["points"][0]["unit"] == "kg"
    assert series["points"][0]["source_type"] == "manual"

    async with postgres.async_session_maker() as session:
        row = (await session.execute(select(HealthMeasurement))).scalar_one()
        assert row.note == "morning" and row.measured_at is not None
