"""spec-076: per-workspace weekly-summary cadence settings."""

import pytest
from httpx import AsyncClient

from app.auth.repository import UserRepository
from app.core.database import postgres
from app.platform.repository import WorkspaceRepository
from app.summaries.repository import WorkspaceSummarySettingRepository
from app.tests.integration.test_spending import _register_and_login


@pytest.mark.asyncio
async def test_summary_cadence_settings_default_get_and_update(client: AsyncClient):
    creds = await _register_and_login(client, "sumcadence")
    cookies = creds["cookies"]

    # No row yet -> the documented default (Monday, hour 1 UTC) that the job
    # itself falls back to when a workspace hasn't configured a cadence.
    default_resp = await client.get("/v1/summaries/weekly/settings", cookies=cookies)
    assert default_resp.status_code == 200, default_resp.text
    assert default_resp.json()["cadence_day_of_week"] == 0
    assert default_resp.json()["cadence_hour_utc"] == 1

    update_resp = await client.put(
        "/v1/summaries/weekly/settings",
        json={"cadence_day_of_week": 3, "cadence_hour_utc": 14},
        cookies=cookies,
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["cadence_day_of_week"] == 3
    assert update_resp.json()["cadence_hour_utc"] == 14

    get_resp = await client.get("/v1/summaries/weekly/settings", cookies=cookies)
    assert get_resp.status_code == 200
    assert get_resp.json()["cadence_day_of_week"] == 3
    assert get_resp.json()["cadence_hour_utc"] == 14


@pytest.mark.asyncio
async def test_summary_cadence_settings_rejects_out_of_range_values(client: AsyncClient):
    creds = await _register_and_login(client, "sumcadencerange")
    cookies = creds["cookies"]

    resp = await client.put(
        "/v1/summaries/weekly/settings",
        json={"cadence_day_of_week": 7, "cadence_hour_utc": 1},
        cookies=cookies,
    )
    assert resp.status_code == 422

    resp2 = await client.put(
        "/v1/summaries/weekly/settings",
        json={"cadence_day_of_week": 0, "cadence_hour_utc": 24},
        cookies=cookies,
    )
    assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_workspace_summary_setting_repository_list_due(client: AsyncClient):
    """Deterministic unit test for the core cadence-gating logic used by
    weekly_summary_job(respect_cadence=True) -- exercised directly against
    explicit (day_of_week, hour_utc) values rather than the wall clock, so
    it isn't flaky at hour/day boundaries."""
    creds = await _register_and_login(client, "sumcadenceduelogic")

    async_session_maker = postgres.get_session_maker(postgres.engine)
    async with async_session_maker() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_username(creds["username"])
        workspace_repo = WorkspaceRepository(session)
        configured_workspace_id = (await workspace_repo.list_user_workspaces(user.id))[0].id

        repo = WorkspaceSummarySettingRepository(session)
        # Configured workspace: due Wednesday (2) at 14:00 UTC.
        await repo.upsert(configured_workspace_id, cadence_day_of_week=2, cadence_hour_utc=14)

        creds2 = await _register_and_login(client, "sumcadencedueother")
        user2 = await user_repo.get_by_username(creds2["username"])
        default_workspace_id = (await workspace_repo.list_user_workspaces(user2.id))[0].id
        # default_workspace_id has no row -> falls back to Monday(0)/hour 1.

        await session.commit()

        # Only the configured workspace is due at (Wed, 14:00).
        due = await repo.list_due(
            [configured_workspace_id, default_workspace_id], day_of_week=2, hour_utc=14
        )
        assert due == [configured_workspace_id]

        # Only the default-cadence workspace is due at (Mon, 01:00).
        due2 = await repo.list_due(
            [configured_workspace_id, default_workspace_id], day_of_week=0, hour_utc=1
        )
        assert due2 == [default_workspace_id]

        # Neither is due at an unrelated slot.
        due3 = await repo.list_due(
            [configured_workspace_id, default_workspace_id], day_of_week=5, hour_utc=9
        )
        assert due3 == []
