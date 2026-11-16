"""Health measurements: any result over time, written through MCP only (spec-111)."""

import uuid
from datetime import date
from typing import Any

from fastmcp import FastMCP

from app.core.audit import AuditLogger
from app.core.database import postgres
from app.core.exceptions import APIError
from app.health.measurements.schemas import MetricDefinition
from app.health.measurements.service import HealthMeasurementError
from app.mcp import security
from app.mcp.support import services

_PREVIEW_MESSAGE = "Preview only, nothing was written. Pass confirmed=true to write."


def _error(message: str) -> dict[str, Any]:
    return {"status": "error", "message": message}


def _parse_day(value: str | None, label: str) -> date | None:
    if value is None or not str(value).strip():
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise HealthMeasurementError(f"{label} must be a YYYY-MM-DD date.") from exc


def _parse_uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise HealthMeasurementError(f"{label} is not a valid id.") from exc


def register(mcp: FastMCP) -> None:
    """Register the health measurement tools on ``mcp``."""

    @mcp.tool
    async def record_health_results(
        workspace_id: int,
        items: list[dict[str, Any]],
        record: dict[str, Any] | None = None,
        atomic: bool = False,
        dry_run: bool = False,
        skip_duplicates: bool = False,
        create_missing_metrics: bool = False,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Write up to 100 results (a lab panel, a prescription, a vitals batch) in one call.

        Without ``confirmed=true`` (or with ``dry_run=true``) nothing is written: the
        call returns what would happen. Always preview first.

        ``record`` is the document the results came from: ``{kind, title, recorded_on?,
        provider?, note?, source_ref?}``. ``kind`` is a slug (``lab_report``,
        ``prescription``, ``imaging``, ``visit``, ``import``...). Put where the data came
        from in ``note`` (free text, 2000 chars). Re-sending the same ``source_ref``
        reuses the record and writes nothing twice.

        Each item: ``{metric, value, unit?, measured_on?, measured_at?, ref_low?,
        ref_high?, ref_text?, note?, source_ref?, category?}``. ``metric`` is a code, name
        or alias from ``list_health_metrics``. ``value`` is a string: ``"5.4"``, ``"<0.5"``,
        ``">100"``, or text for a text metric (``"Negative"``). ``unit`` is required for a
        metric with a canonical unit and must be that unit or a registered conversion; it is
        never guessed. ``ref_low``/``ref_high`` are the report's own range, in the item's unit.
        ``measured_on`` defaults to ``record.recorded_on``.

        An unknown metric is NOT written (status ``unknown_metric`` with suggestions): define
        it with ``define_health_metric`` or pass ``create_missing_metrics=true``. A reading
        that already exists is ``possible_duplicate`` (not written) unless
        ``skip_duplicates=true`` marks it ``duplicate``. ``atomic=true`` rejects the whole
        batch if any item is invalid.

        Returns ``{status, dry_run, record, counts, items:[{index, status, metric,
        match_reason, existing_id, errors, warnings, suggestions}]}``.
        """
        preview = dry_run or not confirmed
        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="record_health_results"
            )
            result = await services.measurement_service(session).record_results(
                user_id=user_id,
                workspace_id=workspace_id,
                record=record,
                items=items,
                atomic=atomic,
                dry_run=preview,
                skip_duplicates=skip_duplicates,
                create_missing_metrics=create_missing_metrics,
                audit_logger=AuditLogger(session),
            )
            if result.get("status") == "success" and not preview:
                await session.commit()
            else:
                await session.rollback()
        result["workspace_id"] = workspace_id
        if preview and not dry_run:
            result["message"] = _PREVIEW_MESSAGE
        return result

    @mcp.tool
    async def define_health_metric(
        workspace_id: int,
        name: str,
        code: str | None = None,
        category: str = "other",
        value_type: str = "numeric",
        canonical_unit: str | None = None,
        unit_conversions: dict[str, Any] | None = None,
        aliases: list[str] | None = None,
        min_value: str | None = None,
        max_value: str | None = None,
    ) -> dict[str, Any]:
        """Create or update one metric in the catalog (a thing that can be measured).

        ``code`` is a slug (``ldl_cholesterol``); omitted, it is derived from ``name``.
        ``value_type`` is ``numeric`` or ``text``. ``canonical_unit`` is the unit series are
        charted in. ``unit_conversions`` maps other units to it:
        ``{"mmol/L": {"factor": "38.67", "offset": "0"}}`` means
        ``canonical = value * factor + offset``. ``aliases`` are other names the metric goes
        by (``["SGPT", "ALT (SGPT)"]``); an alias already used by another metric is rejected.
        ``min_value``/``max_value`` are plausibility bounds (strings). On a metric that already
        has measurements, the canonical unit and value type cannot change and a unit in use
        cannot be dropped.
        """
        try:
            definition = MetricDefinition.model_validate({
                "name": name,
                "code": code,
                "category": category,
                "value_type": value_type,
                "canonical_unit": canonical_unit,
                "unit_conversions": unit_conversions,
                "aliases": aliases,
                "min_value": min_value,
                "max_value": max_value,
            })
        except ValueError as exc:
            return _error(f"Invalid metric definition: {exc}")
        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="define_health_metric"
            )
            service = services.measurement_service(session)
            try:
                metric, outcome = await service.define_metric(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    definition=definition,
                    audit_logger=AuditLogger(session),
                )
                response = service.to_metric_response(metric)
            except HealthMeasurementError as exc:
                await session.rollback()
                return _error(str(exc))
            await session.commit()
        return {
            "status": "success",
            "outcome": outcome,
            "workspace_id": workspace_id,
            "metric": response.model_dump(mode="json"),
        }

    @mcp.tool
    async def list_health_metrics(workspace_id: int) -> dict[str, Any]:
        """The catalog of measurable things in a workspace, with how many results each has
        and its latest result. ``body_weight`` appears once weight (or any result) has been
        written. Call this before
        ``record_health_results`` to pick the right metric names and units.
        """
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="list_health_metrics"
            )
            metrics = await services.measurement_service(session).list_metrics(workspace_id)
        return {
            "status": "success",
            "workspace_id": workspace_id,
            "items": [m.model_dump(mode="json") for m in metrics],
            "total": len(metrics),
        }

    @mcp.tool
    async def get_health_series(
        workspace_id: int,
        metric: str,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """One metric over time, newest first, with stats.

        ``metric`` is a code, name or alias. ``from_date``/``to_date`` are inclusive
        YYYY-MM-DD days. Each point has the value and unit as reported, the value converted
        to the metric's canonical unit, any ``<``/``>`` comparator, the report's own range and
        ``in_range`` (``low``/``normal``/``high`` against that range only), plus its record and
        note. ``stats`` (latest, min, max, 7- and 30-day change) use canonical units and
        exclude threshold and text rows. ``limit`` is capped at 100.
        """
        security.validate_page(limit, offset)
        try:
            from_on = _parse_day(from_date, "from_date")
            to_on = _parse_day(to_date, "to_date")
        except HealthMeasurementError as exc:
            return _error(str(exc))
        if from_on and to_on and from_on > to_on:
            return _error("from_date must not be after to_date.")
        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="get_health_series"
            )
            try:
                series = await services.measurement_service(session).get_series(
                    workspace_id,
                    metric,
                    from_on=from_on,
                    to_on=to_on,
                    limit=limit,
                    offset=offset,
                    user_id=user_id,
                )
            except APIError as exc:
                return _error(exc.detail)
        return {
            "status": "success",
            "workspace_id": workspace_id,
            **series.model_dump(mode="json"),
        }

    @mcp.tool
    async def list_health_records(
        workspace_id: int,
        record_id: str | None = None,
        kind: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Records (lab reports, prescriptions, imports...) newest first, each with its
        provider, free note, ``source_ref`` and result count. Pass ``record_id`` (a record's
        ``public_id``) to get that one record with every measurement in it. ``limit`` is
        capped at 100.
        """
        security.validate_page(limit, offset)
        try:
            from_on = _parse_day(from_date, "from_date")
            to_on = _parse_day(to_date, "to_date")
            record_uuid = _parse_uuid(record_id, "record_id") if record_id else None
        except HealthMeasurementError as exc:
            return _error(str(exc))
        async with postgres.async_session_maker() as session:
            await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:read", tool="list_health_records"
            )
            service = services.measurement_service(session)
            try:
                if record_uuid is not None:
                    detail = await service.get_record(workspace_id, record_uuid)
                    return {
                        "status": "success",
                        "workspace_id": workspace_id,
                        "record": detail.model_dump(mode="json"),
                    }
                items, total = await service.list_records(
                    workspace_id,
                    kind=kind.strip().lower() if kind else None,
                    from_on=from_on,
                    to_on=to_on,
                    limit=limit,
                    offset=offset,
                )
            except APIError as exc:
                return _error(exc.detail)
        return {
            "status": "success",
            "workspace_id": workspace_id,
            "items": [r.model_dump(mode="json") for r in items],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
        }

    @mcp.tool
    async def delete_health_measurement(
        workspace_id: int, public_id: str, confirmed: bool = False
    ) -> dict[str, Any]:
        """Delete one measurement by its ``public_id``. Corrections are delete + re-log.
        Without ``confirmed=true`` nothing is deleted.
        """
        try:
            target = _parse_uuid(public_id, "public_id")
        except HealthMeasurementError as exc:
            return _error(str(exc))
        if not confirmed:
            return {
                "status": "preview",
                "deleted": False,
                "message": "Pass confirmed=true to delete.",
            }
        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="delete_health_measurement"
            )
            try:
                await services.measurement_service(session).delete_measurement(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    public_id=target,
                    audit_logger=AuditLogger(session),
                )
            except APIError as exc:
                await session.rollback()
                return _error(exc.detail)
            await session.commit()
        return {"status": "success", "deleted": True, "workspace_id": workspace_id}

    @mcp.tool
    async def delete_health_record(
        workspace_id: int, public_id: str, confirmed: bool = False
    ) -> dict[str, Any]:
        """Delete a record and ALL of its measurements (the undo for a mis-extracted report).
        Without ``confirmed=true`` it only reports how many measurements it would remove.
        """
        try:
            target = _parse_uuid(public_id, "public_id")
        except HealthMeasurementError as exc:
            return _error(str(exc))
        async with postgres.async_session_maker() as session:
            user_id = await security.authorize_workspace(
                session, workspace_id, required_scope="mcp:write", tool="delete_health_record"
            )
            try:
                result = await services.measurement_service(session).delete_record(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    public_id=target,
                    confirmed=confirmed,
                    audit_logger=AuditLogger(session),
                )
            except APIError as exc:
                await session.rollback()
                return _error(exc.detail)
            if result.get("deleted"):
                await session.commit()
            else:
                await session.rollback()
        return {"workspace_id": workspace_id, **result}
