"""Read-only REST for the health measurement store (spec-110 §E).

There are deliberately no generic write routes: ingestion is MCP-only. Weight keeps its own
read/write routes in ``app/health/router.py`` (served by the same store).
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import (
    get_current_user,
    get_current_workspace_id,
    get_measurement_service,
)
from app.core.pagination import PaginatedResponse, PaginationParams, build_page
from app.health.measurements.schemas import (
    MetricResponse,
    RecordDetailResponse,
    RecordResponse,
    SeriesResponse,
)
from app.health.measurements.service import MeasurementService

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/metrics", response_model=list[MetricResponse])
async def list_metrics(
    service: Annotated[MeasurementService, Depends(get_measurement_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    return await service.list_metrics(workspace_id)


@router.get("/metrics/{ref}/series", response_model=SeriesResponse)
async def get_metric_series(
    ref: str,
    service: Annotated[MeasurementService, Depends(get_measurement_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
):
    return await service.get_series(
        workspace_id,
        ref,
        from_on=from_date,
        to_on=to_date,
        limit=pagination.limit,
        offset=pagination.offset,
        user_id=user["id"],
    )


@router.get("/records", response_model=PaginatedResponse[RecordResponse])
async def list_records(
    service: Annotated[MeasurementService, Depends(get_measurement_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    kind: str | None = Query(None),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
):
    items, total = await service.list_records(
        workspace_id,
        kind=kind,
        from_on=from_date,
        to_on=to_date,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return build_page(items, total, pagination)


@router.get("/records/{public_id}", response_model=RecordDetailResponse)
async def get_record(
    public_id: uuid.UUID,
    service: Annotated[MeasurementService, Depends(get_measurement_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    return await service.get_record(workspace_id, public_id)
