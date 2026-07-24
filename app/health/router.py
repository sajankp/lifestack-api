import uuid
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.config import settings
from app.core.audit import AuditLogger
from app.core.dependencies import (
    get_audit_logger,
    get_current_user,
    get_current_workspace_id,
    get_health_service,
    require_min_role,
)
from app.core.pagination import PaginatedResponse, PaginationParams, build_page
from app.health.schemas import (
    DoseSlot,
    MedicationCreate,
    MedicationEventResponse,
    MedicationEventUpsert,
    MedicationResponse,
    MedicationUpdate,
    WeightEntryCreate,
    WeightEntryResponse,
    WeightTrendResponse,
)
from app.health.service import HealthService

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/medications", response_model=PaginatedResponse[MedicationResponse])
async def list_medications(
    health_service: Annotated[HealthService, Depends(get_health_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    is_active: bool | None = Query(None),
):
    items, total = await health_service.list_medications(
        workspace_id, is_active, pagination.limit, pagination.offset
    )
    return build_page(items, total, pagination)


@router.post("/medications", response_model=MedicationResponse, status_code=status.HTTP_201_CREATED)
async def create_medication(
    payload: MedicationCreate,
    health_service: Annotated[HealthService, Depends(get_health_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    med = await health_service.create_medication(
        user["id"], workspace_id, payload, audit_logger=audit_logger
    )
    return await health_service.get_medication_response(workspace_id, med.public_id)


@router.get("/medications/schedule", response_model=list[DoseSlot])
async def get_medication_schedule(
    health_service: Annotated[HealthService, Depends(get_health_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    date_: Annotated[date, Query(alias="date")],
):
    return await health_service.get_schedule(workspace_id, date_)


@router.get("/medications/overdue", response_model=list[DoseSlot])
async def get_overdue_doses(
    health_service: Annotated[HealthService, Depends(get_health_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    lookback_days: int = Query(default=settings.HEALTH_CATCH_UP_LOOKBACK_DAYS, ge=1, le=30),
):
    return await health_service.get_overdue_slots(workspace_id, lookback_days)


@router.get("/medications/{medication_id}", response_model=MedicationResponse)
async def get_medication(
    medication_id: uuid.UUID,
    health_service: Annotated[HealthService, Depends(get_health_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    return await health_service.get_medication_response(workspace_id, medication_id)


@router.patch("/medications/{medication_id}", response_model=MedicationResponse)
async def update_medication(
    medication_id: uuid.UUID,
    payload: MedicationUpdate,
    health_service: Annotated[HealthService, Depends(get_health_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await health_service.update_medication(
        workspace_id, medication_id, payload, actor_id=user["id"], audit_logger=audit_logger
    )
    return await health_service.get_medication_response(workspace_id, medication_id)


@router.delete("/medications/{medication_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_medication(
    medication_id: uuid.UUID,
    health_service: Annotated[HealthService, Depends(get_health_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await health_service.delete_medication(
        workspace_id, medication_id, actor_id=user["id"], audit_logger=audit_logger
    )


@router.put("/medications/{medication_id}/events", response_model=MedicationEventResponse)
async def upsert_medication_event(
    medication_id: uuid.UUID,
    payload: MedicationEventUpsert,
    health_service: Annotated[HealthService, Depends(get_health_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    event = await health_service.upsert_event(
        user["id"], workspace_id, medication_id, payload, audit_logger=audit_logger
    )
    return MedicationEventResponse(
        public_id=event.public_id,
        medication_public_id=medication_id,
        scheduled_for=event.scheduled_for,
        status=event.status,
        logged_at=event.logged_at,
        taken_at=event.taken_at,
        note=event.note,
        source_type=event.source_type,
    )


@router.get("/weight", response_model=PaginatedResponse[WeightEntryResponse])
async def list_weight_entries(
    health_service: Annotated[HealthService, Depends(get_health_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    start: date | None = Query(None),
    end: date | None = Query(None),
):
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=UTC) if start else None
    end_dt = datetime.combine(end, datetime.max.time(), tzinfo=UTC) if end else None
    items, total = await health_service.list_weight_entries(
        workspace_id, start_dt, end_dt, pagination.limit, pagination.offset
    )
    return build_page(items, total, pagination)


@router.post("/weight", response_model=WeightEntryResponse, status_code=status.HTTP_201_CREATED)
async def create_weight_entry(
    payload: WeightEntryCreate,
    health_service: Annotated[HealthService, Depends(get_health_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    entry = await health_service.create_weight_entry(
        user["id"], workspace_id, payload, audit_logger=audit_logger
    )
    return WeightEntryResponse.model_validate(entry)


@router.get("/weight/trend", response_model=WeightTrendResponse)
async def get_weight_trend(
    health_service: Annotated[HealthService, Depends(get_health_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    days: int = Query(30, ge=1, le=365),
):
    return await health_service.get_weight_trend(workspace_id, days)


@router.delete("/weight/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_weight_entry(
    entry_id: uuid.UUID,
    health_service: Annotated[HealthService, Depends(get_health_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await health_service.delete_weight_entry(
        workspace_id, entry_id, actor_id=user["id"], audit_logger=audit_logger
    )
