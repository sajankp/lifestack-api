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


@pytest.mark.asyncio
async def test_medication_create_supports_legacy_and_standard_timezones(client: AsyncClient):
    await _register_and_login(client, "tzuser")

    # Test legacy/deprecated timezone name 'Asia/Calcutta'
    payload_legacy = {
        "name": "Elicit DS",
        "dose_text": "20",
        "frequency": "daily",
        "interval": 1,
        "anchor_date": "2026-07-09",
        "timezone": "Asia/Calcutta",
        "times": ["21:00"],
    }
    res_legacy = await client.post("/v1/health/medications", json=payload_legacy)
    assert res_legacy.status_code == 201, res_legacy.text
    assert res_legacy.json()["timezone"] == "Asia/Calcutta"

    # Test standard canonical timezone name 'Asia/Kolkata'
    payload_standard = {
        "name": "Elicit DS 2",
        "dose_text": "20",
        "frequency": "daily",
        "interval": 1,
        "anchor_date": "2026-07-09",
        "timezone": "Asia/Kolkata",
        "times": ["21:00"],
    }
    res_standard = await client.post("/v1/health/medications", json=payload_standard)
    assert res_standard.status_code == 201, res_standard.text
    assert res_standard.json()["timezone"] == "Asia/Kolkata"


# ---- spec-092: interval scheduling + catch-up ----------------------------


@pytest.mark.asyncio
async def test_interval_mode_rejects_non_daily(client: AsyncClient):
    await _register_and_login(client, "intervalvaliduser")
    payload = {
        "name": "Interval Weekly",
        "frequency": "weekly",
        "schedule_mode": "interval_from_last_dose",
        "anchor_date": "2026-01-01",
        "timezone": "UTC",
        "times": ["09:00"],
        "days_of_week": [0],
    }
    res = await client.post("/v1/health/medications", json=payload)
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
async def test_interval_mode_reanchors_after_late_take(client: AsyncClient):
    await _register_and_login(client, "intervaluser")
    today = datetime.now(UTC).date()
    payload = {
        "name": "Interval Med",
        "frequency": "daily",
        "interval": 2,
        "schedule_mode": "interval_from_last_dose",
        "anchor_date": today.isoformat(),
        "timezone": "UTC",
        "times": ["00:01"],
    }
    create_res = await client.post("/v1/health/medications", json=payload)
    assert create_res.status_code == 201, create_res.text
    assert create_res.json()["schedule_mode"] == "interval_from_last_dose"
    med_id = create_res.json()["public_id"]

    # First dose is due today (anchor); +1 day is an off day.
    slots_today = (
        await client.get("/v1/health/medications/schedule", params={"date": today.isoformat()})
    ).json()
    assert len(slots_today) == 1
    next_day = (today + timedelta(days=1)).isoformat()
    assert (
        await client.get("/v1/health/medications/schedule", params={"date": next_day})
    ).json() == []

    # Take today's dose "now" — next due re-anchors to today + interval (2 days).
    upsert = await client.put(
        f"/v1/health/medications/{med_id}/events",
        json={"scheduled_for": slots_today[0]["scheduled_for"], "status": "taken"},
    )
    assert upsert.status_code == 200
    assert upsert.json()["taken_at"] is not None  # defaulted to now

    due_date = (today + timedelta(days=2)).isoformat()
    slots_due = (
        await client.get("/v1/health/medications/schedule", params={"date": due_date})
    ).json()
    assert len(slots_due) == 1
    assert slots_due[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_skipped_event_clears_taken_at(client: AsyncClient):
    await _register_and_login(client, "takenatuser")
    today = datetime.now(UTC).date()
    med_id = (
        await client.post(
            "/v1/health/medications",
            json={
                "name": "Aspirin",
                "frequency": "daily",
                "anchor_date": today.isoformat(),
                "timezone": "UTC",
                "times": ["00:01"],
            },
        )
    ).json()["public_id"]
    slot = (
        await client.get("/v1/health/medications/schedule", params={"date": today.isoformat()})
    ).json()[0]["scheduled_for"]

    taken = await client.put(
        f"/v1/health/medications/{med_id}/events",
        json={"scheduled_for": slot, "status": "taken"},
    )
    assert taken.json()["taken_at"] is not None
    skipped = await client.put(
        f"/v1/health/medications/{med_id}/events",
        json={"scheduled_for": slot, "status": "skipped"},
    )
    assert skipped.json()["taken_at"] is None


@pytest.mark.asyncio
async def test_overdue_endpoint_returns_missed_doses(client: AsyncClient):
    await _register_and_login(client, "overdueuser")
    today = datetime.now(UTC).date()
    # Daily med anchored 3 days ago, dose at 00:01 → yesterday's slot is past grace.
    med_id = (
        await client.post(
            "/v1/health/medications",
            json={
                "name": "Daily Med",
                "frequency": "daily",
                "anchor_date": (today - timedelta(days=3)).isoformat(),
                "timezone": "UTC",
                "times": ["00:01"],
            },
        )
    ).json()["public_id"]

    overdue = await client.get("/v1/health/medications/overdue", params={"lookback_days": 7})
    assert overdue.status_code == 200
    slots = overdue.json()
    assert len(slots) >= 1
    assert all(s["status"] == "missed" for s in slots)
    assert all(s["medication_public_id"] == med_id for s in slots)
    # Newest-first ordering.
    ordered = [s["scheduled_for"] for s in slots]
    assert ordered == sorted(ordered, reverse=True)

    # Answering the most recent missed slot removes it from the catch-up list.
    answered_before = len(slots)
    await client.put(
        f"/v1/health/medications/{med_id}/events",
        json={"scheduled_for": slots[0]["scheduled_for"], "status": "taken"},
    )
    overdue_after = (
        await client.get("/v1/health/medications/overdue", params={"lookback_days": 7})
    ).json()
    assert len(overdue_after) == answered_before - 1
