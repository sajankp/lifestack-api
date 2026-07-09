from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient


async def _register_and_login(client: AsyncClient, username: str) -> None:
    user = {"email": f"{username}@example.com", "username": username, "password": "TestPass123!"}
    res = await client.post("/v1/auth/register", json=user)
    assert res.status_code == 200
    res = await client.post(
        "/v1/auth/login", data={"username": username, "password": user["password"]}
    )
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_medication_crud_flow_is_workspace_scoped(client: AsyncClient):
    await _register_and_login(client, "healthuser")

    payload = {
        "name": "Metformin",
        "dose_text": "500 mg",
        "frequency": "daily",
        "interval": 1,
        "anchor_date": "2026-01-01",
        "timezone": "UTC",
        "times": ["09:00"],
    }
    create_res = await client.post("/v1/health/medications", json=payload)
    assert create_res.status_code == 201, create_res.text
    med = create_res.json()
    assert med["name"] == "Metformin"
    assert med["event_count"] == 0
    med_id = med["public_id"]

    list_res = await client.get("/v1/health/medications")
    assert list_res.status_code == 200
    assert list_res.json()["total"] == 1

    get_res = await client.get(f"/v1/health/medications/{med_id}")
    assert get_res.status_code == 200

    patch_res = await client.patch(
        f"/v1/health/medications/{med_id}", json={"dose_text": "1000 mg"}
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["dose_text"] == "1000 mg"

    await client.post("/v1/auth/logout")
    await _register_and_login(client, "otherhealthuser")

    cross_get = await client.get(f"/v1/health/medications/{med_id}")
    assert cross_get.status_code == 404

    cross_delete = await client.delete(f"/v1/health/medications/{med_id}")
    assert cross_delete.status_code == 404


@pytest.mark.asyncio
async def test_medication_create_rejects_days_of_week_outside_weekly(client: AsyncClient):
    await _register_and_login(client, "healthvaliduser")
    payload = {
        "name": "Vitamin D",
        "frequency": "daily",
        "anchor_date": "2026-01-01",
        "timezone": "UTC",
        "times": ["09:00"],
        "days_of_week": [0, 2],
    }
    res = await client.post("/v1/health/medications", json=payload)
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_medication_create_requires_days_of_week_when_weekly(client: AsyncClient):
    await _register_and_login(client, "healthweeklyuser")
    payload = {
        "name": "Weekly Med",
        "frequency": "weekly",
        "anchor_date": "2026-01-01",
        "timezone": "UTC",
        "times": ["09:00"],
    }
    res = await client.post("/v1/health/medications", json=payload)
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_schedule_and_event_upsert_flow(client: AsyncClient):
    await _register_and_login(client, "scheduleuser")

    today = datetime.now(UTC).date()
    payload = {
        "name": "Aspirin",
        "dose_text": "75 mg",
        "frequency": "daily",
        "interval": 1,
        "anchor_date": today.isoformat(),
        "timezone": "UTC",
        "times": ["00:01"],
    }
    create_res = await client.post("/v1/health/medications", json=payload)
    assert create_res.status_code == 201
    med_id = create_res.json()["public_id"]

    schedule_res = await client.get(
        "/v1/health/medications/schedule", params={"date": today.isoformat()}
    )
    assert schedule_res.status_code == 200
    slots = schedule_res.json()
    assert len(slots) == 1
    slot = slots[0]
    assert slot["medication_public_id"] == med_id
    assert slot["status"] in ("pending", "missed")

    upsert_res = await client.put(
        f"/v1/health/medications/{med_id}/events",
        json={"scheduled_for": slot["scheduled_for"], "status": "taken", "note": "with food"},
    )
    assert upsert_res.status_code == 200
    event = upsert_res.json()
    assert event["status"] == "taken"
    assert event["note"] == "with food"

    schedule_res2 = await client.get(
        "/v1/health/medications/schedule", params={"date": today.isoformat()}
    )
    assert schedule_res2.json()[0]["status"] == "taken"

    # Re-logging the same slot updates rather than duplicates.
    upsert_res2 = await client.put(
        f"/v1/health/medications/{med_id}/events",
        json={"scheduled_for": slot["scheduled_for"], "status": "skipped"},
    )
    assert upsert_res2.status_code == 200
    assert upsert_res2.json()["status"] == "skipped"

    med_get = await client.get(f"/v1/health/medications/{med_id}")
    assert med_get.json()["event_count"] == 1


@pytest.mark.asyncio
async def test_medication_delete_cascades_events(client: AsyncClient):
    await _register_and_login(client, "cascadeuser")
    today = datetime.now(UTC).date()
    payload = {
        "name": "Ibuprofen",
        "frequency": "daily",
        "anchor_date": today.isoformat(),
        "timezone": "UTC",
        "times": ["00:01"],
    }
    create_res = await client.post("/v1/health/medications", json=payload)
    med_id = create_res.json()["public_id"]

    schedule_res = await client.get(
        "/v1/health/medications/schedule", params={"date": today.isoformat()}
    )
    slot = schedule_res.json()[0]
    await client.put(
        f"/v1/health/medications/{med_id}/events",
        json={"scheduled_for": slot["scheduled_for"], "status": "taken"},
    )

    delete_res = await client.delete(f"/v1/health/medications/{med_id}")
    assert delete_res.status_code == 204

    get_res = await client.get(f"/v1/health/medications/{med_id}")
    assert get_res.status_code == 404


@pytest.mark.asyncio
async def test_weight_crud_and_trend(client: AsyncClient):
    await _register_and_login(client, "weightuser")

    now = datetime.now(UTC)
    entry1 = await client.post(
        "/v1/health/weight",
        json={
            "measured_at": (now - timedelta(days=10)).isoformat(),
            "weight_kg": "80.0",
        },
    )
    assert entry1.status_code == 201

    entry2 = await client.post(
        "/v1/health/weight",
        json={"measured_at": now.isoformat(), "weight_kg": "79.0"},
    )
    assert entry2.status_code == 201
    entry2_id = entry2.json()["public_id"]

    list_res = await client.get("/v1/health/weight")
    assert list_res.status_code == 200
    assert list_res.json()["total"] == 2

    trend_res = await client.get("/v1/health/weight/trend", params={"days": 30})
    assert trend_res.status_code == 200
    trend = trend_res.json()
    assert trend["latest_kg"] == "79.00"
    assert trend["min_kg"] == "79.00"
    assert trend["max_kg"] == "80.00"
    assert trend["delta_7d_kg"] == "-1.00"

    delete_res = await client.delete(f"/v1/health/weight/{entry2_id}")
    assert delete_res.status_code == 204

    list_res2 = await client.get("/v1/health/weight")
    assert list_res2.json()["total"] == 1


@pytest.mark.asyncio
async def test_weight_entry_rejects_non_positive(client: AsyncClient):
    await _register_and_login(client, "weightbaduser")
    res = await client.post(
        "/v1/health/weight",
        json={"measured_at": datetime.now(UTC).isoformat(), "weight_kg": "-5.0"},
    )
    assert res.status_code == 422
