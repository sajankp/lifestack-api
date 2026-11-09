"""Weight and medication tracking."""

import uuid
from typing import Any

from fastmcp import FastMCP

from app.core.database import postgres
from app.health.repository import MedicationEventRepository, MedicationRepository
from app.mcp import security
from app.mcp.support import common, services


def register(mcp: FastMCP) -> None:
    """Register the health tools on ``mcp``."""

    @mcp.tool
    async def log_weight(
        workspace_id: int, weight_kg: str, note: str | None = None
    ) -> dict[str, Any]:
        """Log a body-weight measurement in an authorized workspace."""
        return await common.run_capture_tool(
            workspace_id,
            "mcp:write",
            "log_weight",
            {"weight_kg": weight_kg, "note": note},
        )

    @mcp.tool
    async def log_medication_event(
        workspace_id: int,
        name: str,
        status: str,
        dose_time: str | None = None,
    ) -> dict[str, Any]:
        """Record a medication event in an authorized workspace."""
        return await common.run_capture_tool(
            workspace_id,
            "mcp:write",
            "log_medication_event",
            {"name": name, "status": status, "dose_time": dose_time},
        )

    @mcp.tool
    async def get_weight_history(
        workspace_id: int,
        start: str | None = None,
        end: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Paginated body-weight history, newest first (read counterpart of log_weight).

        ``start``/``end`` are ISO-8601 bounds on ``measured_at`` (naive values
        are read as UTC). ``limit`` is capped at 100. Weights are strings in kg.
        """
        security.validate_page(limit, offset)
        try:
            start_dt = common.parse_iso_datetime(start, "start")
            end_dt = common.parse_iso_datetime(end, "end")
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        if start_dt and end_dt and start_dt > end_dt:
            return {"status": "error", "message": "start must not be after end."}
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_weight_history"
            )
            items, total = await services.health_service(session).list_weight_entries(
                workspace_id, start_dt, end_dt, limit, offset
            )
        return {
            "status": "success",
            "workspace_id": workspace_id,
            "items": [item.model_dump(mode="json") for item in items],
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < int(total),
        }

    @mcp.tool
    async def list_medication_events(
        workspace_id: int,
        medication_id: str | None = None,
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Paginated medication dose-event history, newest first (read counterpart
        of log_medication_event). Filter by ``medication_id`` (UUID) and/or
        ``status`` (``taken`` | ``skipped``). ``limit`` is capped at 100.
        """
        security.validate_page(limit, offset)
        status_filter = status.strip().lower() if status else None
        if status_filter is not None and status_filter not in {"taken", "skipped"}:
            return {"status": "error", "message": "status must be 'taken' or 'skipped'."}
        try:
            medication_public_id = uuid.UUID(medication_id) if medication_id else None
        except ValueError:
            return {"status": "error", "message": "Invalid medication_id."}
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="list_medication_events"
            )
            medication_repo = MedicationRepository(session)
            medication_internal_id: int | None = None
            if medication_public_id is not None:
                medication = await medication_repo.get_by_public_id(
                    workspace_id, medication_public_id
                )
                if medication is None:
                    return {"status": "error", "message": "Medication not found in this workspace."}
                medication_internal_id = medication.id
            events, total = await MedicationEventRepository(session).list_by_workspace(
                workspace_id,
                medication_id=medication_internal_id,
                status=status_filter,
                limit=limit,
                offset=offset,
            )
            medications = await medication_repo.get_by_ids(
                workspace_id, [e.medication_id for e in events]
            )
        return {
            "status": "success",
            "workspace_id": workspace_id,
            "medication_id": medication_id,
            "status_filter": status_filter,
            "items": [
                {
                    "public_id": str(e.public_id),
                    "medication_public_id": str(medications[e.medication_id].public_id)
                    if e.medication_id in medications
                    else None,
                    "medication_name": medications[e.medication_id].name
                    if e.medication_id in medications
                    else None,
                    "scheduled_for": e.scheduled_for.isoformat(),
                    "status": e.status,
                    "logged_at": e.logged_at.isoformat(),
                    "taken_at": e.taken_at.isoformat() if e.taken_at else None,
                    "note": e.note,
                    "source_type": e.source_type,
                }
                for e in events
            ],
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(events) < int(total),
        }
