"""
Integration tests for Export creation, retrieval, download, and deletion lifecycle.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.auth.models import User
from app.core.database import postgres
from app.exports.models import ExportFormat, ExportRecord, ExportStatus
from app.platform.models import WorkspaceMembership


async def _register_and_login(client: AsyncClient, suffix: str):
    username = f"export_{suffix}"
    email = f"{username}@example.com"
    password = "Password123!"
    reg = await client.post(
        "/v1/auth/register", json={"email": email, "username": username, "password": password}
    )
    assert reg.status_code == 200
    login = await client.post("/v1/auth/login", data={"username": username, "password": password})
    assert login.status_code == 200

    # Extract user ID and default workspace ID by finding the user in the DB
    async with postgres.async_session_maker() as session:
        user_res = await session.execute(select(User).where(User.username == username))
        user = user_res.scalar_one()

        membership_res = await session.execute(
            select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id)
        )
        membership = membership_res.scalar_one()

        return {
            "user_id": user.id,
            "workspace_id": membership.workspace_id,
            "cookies": dict(login.cookies),
        }


@pytest.mark.asyncio
async def test_export_lifecycle_create_get_download_delete(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    cookies = creds["cookies"]

    # 1. Create Export
    create_resp = await client.post(
        "/v1/exports",
        json={"format": "json", "modules": ["todo"]},
        cookies=cookies,
    )
    assert create_resp.status_code == 201
    export = create_resp.json()
    assert export["status"] == ExportStatus.ready
    export_id = export["public_id"]

    # 2. Get Export status
    get_resp = await client.get(f"/v1/exports/{export_id}", cookies=cookies)
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == ExportStatus.ready

    # 3. Download Export
    download_resp = await client.get(f"/v1/exports/{export_id}/download", cookies=cookies)
    assert download_resp.status_code == 200
    assert download_resp.headers["content-type"] == "application/json"
    assert "todos" in download_resp.json()["data"]["todo"]

    # 4. Delete Export
    delete_resp = await client.delete(f"/v1/exports/{export_id}", cookies=cookies)
    assert delete_resp.status_code == 204

    # 5. Verify deleted export is not found
    get_after_delete = await client.get(f"/v1/exports/{export_id}", cookies=cookies)
    assert get_after_delete.status_code == 404


@pytest.mark.asyncio
async def test_delete_pending_export_raises_conflict(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    cookies = creds["cookies"]

    # Create a pending export record directly in DB
    async with postgres.async_session_maker() as session:
        pending_record = ExportRecord(
            workspace_id=creds["workspace_id"],
            requested_by=creds["user_id"],
            format=ExportFormat.json,
            schema_version=1,
            scope={"modules": ["todo"]},
            status=ExportStatus.pending,
        )
        session.add(pending_record)
        await session.commit()
        await session.refresh(pending_record)
        export_public_id = pending_record.public_id

    # Try to delete via API -> should conflict
    delete_resp = await client.delete(f"/v1/exports/{export_public_id}", cookies=cookies)
    assert delete_resp.status_code == 409
    assert "still being generated" in delete_resp.json()["detail"].lower()

    # Clean up DB record
    async with postgres.async_session_maker() as session:
        record_to_del = await session.get(ExportRecord, pending_record.id)
        if record_to_del:
            await session.delete(record_to_del)
            await session.commit()
