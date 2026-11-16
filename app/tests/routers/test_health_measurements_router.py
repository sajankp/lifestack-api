"""Read-only REST routes for the measurement store (spec-110 §E)."""

import pytest
from httpx import AsyncClient

from app.auth.repository import UserRepository
from app.core.database import postgres
from app.health.measurements.schemas import MetricDefinition
from app.health.measurements.service import MeasurementService
from app.platform.repository import WorkspaceRepository
from app.tests.integration.test_spending import _register_and_login


async def _seed(client: AsyncClient, username_tag: str) -> dict:
    """Register a user and write one lab report through the service (the MCP write path)."""
    creds = await _register_and_login(client, username_tag)
    async_session_maker = postgres.get_session_maker(postgres.engine)
    async with async_session_maker() as session:
        user = await UserRepository(session).get_by_username(creds["username"])
        workspace_id = (await WorkspaceRepository(session).list_user_workspaces(user.id))[0].id
        service = MeasurementService.from_session(session)
        await service.define_metric(
            user_id=user.id,
            workspace_id=workspace_id,
            definition=MetricDefinition(
                name="LDL Cholesterol",
                code="ldl_cholesterol",
                category="lipid",
                canonical_unit="mg/dL",
                unit_conversions={"mmol/L": {"factor": "38.67", "offset": "0"}},
                aliases=["LDL-C"],
            ),
        )
        out = await service.record_results(
            user_id=user.id,
            workspace_id=workspace_id,
            record={
                "kind": "lab_report",
                "title": "Lipid panel",
                "recorded_on": "2026-11-10",
                "provider": "Metropolis",
                "note": "Extracted from the emailed PDF",
                "source_ref": f"lab-{username_tag}",
            },
            items=[
                {
                    "metric": "LDL-C",
                    "value": "4.0",
                    "unit": "mmol/L",
                    "ref_high": "3.4",
                    "note": "fasting",
                },
                {"metric": "LDL-C", "value": "<20", "unit": "mg/dL", "measured_on": "2026-11-01"},
            ],
            dry_run=False,
        )
        assert out["status"] == "success", out
        await session.commit()
    return {**creds, "record_id": out["record"]["public_id"]}


@pytest.mark.asyncio
async def test_metrics_series_and_records_round_trip(client: AsyncClient):
    seeded = await _seed(client, "hmrest1")

    metrics = (await client.get("/v1/health/metrics")).json()
    by_code = {m["code"]: m for m in metrics}
    assert set(by_code) == {"body_weight", "ldl_cholesterol"}
    ldl = by_code["ldl_cholesterol"]
    assert ldl["measurement_count"] == 2
    assert ldl["canonical_unit"] == "mg/dL"
    assert ldl["unit_conversions"] == {"mmol/L": {"factor": "38.670000", "offset": "0"}} or ldl[
        "unit_conversions"
    ]["mmol/L"]["factor"].startswith("38.67")
    assert ldl["latest"]["measured_on"] == "2026-11-10"

    res = await client.get("/v1/health/metrics/ldl-c/series", params={"limit": 10})
    assert res.status_code == 200
    series = res.json()
    assert series["total"] == 2 and series["has_more"] is False
    newest, older = series["points"]
    assert newest["unit"] == "mmol/L" and newest["converted"] is True
    assert newest["in_range"] == "high" and newest["ref_high"] == "3.400000"
    assert newest["note"] == "fasting" and newest["source_type"] == "mcp"
    assert newest["record_public_id"] == seeded["record_id"]
    assert older["comparator"] == "<" and older["in_range"] is None
    assert series["stats"]["latest_on"] == "2026-11-10"
    assert set(newest) >= {
        "public_id",
        "measured_on",
        "measured_at",
        "value",
        "comparator",
        "value_text",
        "unit",
        "canonical_value",
        "converted",
        "ref_low",
        "ref_high",
        "ref_text",
        "in_range",
        "record_public_id",
        "note",
        "source_type",
        "source_ref",
    }

    ranged = await client.get(
        "/v1/health/metrics/ldl_cholesterol/series",
        params={"from_date": "2026-11-05", "to_date": "2026-11-30"},
    )
    assert ranged.json()["total"] == 1

    records = (await client.get("/v1/health/records")).json()
    assert records["total"] == 1
    record = records["items"][0]
    assert record["note"] == "Extracted from the emailed PDF"
    assert record["provider"] == "Metropolis" and record["measurement_count"] == 2

    detail = (await client.get(f"/v1/health/records/{seeded['record_id']}")).json()
    assert detail["title"] == "Lipid panel"
    assert {m["metric_code"] for m in detail["measurements"]} == {"ldl_cholesterol"}

    kinds = await client.get("/v1/health/records", params={"kind": "prescription"})
    assert kinds.json()["total"] == 0


@pytest.mark.asyncio
async def test_unknown_metric_and_record_are_404(client: AsyncClient):
    await _seed(client, "hmrest2")
    assert (await client.get("/v1/health/metrics/nope/series")).status_code == 404
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(f"/v1/health/records/{missing}")).status_code == 404
    assert (await client.get("/v1/health/records/not-a-uuid")).status_code == 422
    assert (await client.get("/v1/health/records", params={"limit": 0})).status_code == 422


@pytest.mark.asyncio
async def test_another_users_workspace_cannot_see_the_data(client: AsyncClient):
    seeded = await _seed(client, "hmrest3")
    await client.post("/v1/auth/logout")
    await _register_and_login(client, "hmrest4")

    metrics = (await client.get("/v1/health/metrics")).json()
    assert metrics == []  # nothing of the other workspace's, and a read never creates metrics
    assert (await client.get("/v1/health/metrics/ldl_cholesterol/series")).status_code == 404
    assert (await client.get(f"/v1/health/records/{seeded['record_id']}")).status_code == 404
    assert (await client.get("/v1/health/records")).json()["total"] == 0


@pytest.mark.asyncio
async def test_there_is_no_generic_write_route(client: AsyncClient):
    await _register_and_login(client, "hmrest5")
    for path in ("/v1/health/metrics", "/v1/health/records"):
        res = await client.post(path, json={})
        assert res.status_code == 405, (path, res.status_code)


@pytest.mark.asyncio
async def test_unauthenticated_requests_are_rejected(client: AsyncClient):
    for path in ("/v1/health/metrics", "/v1/health/records", "/v1/health/metrics/x/series"):
        assert (await client.get(path)).status_code == 401
