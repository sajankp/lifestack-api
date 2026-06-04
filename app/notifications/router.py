import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.dependencies import (
    get_current_user,
    get_current_workspace_id,
    get_notification_service,
    require_min_role,
)
from app.core.pagination import PaginatedResponse, PaginationParams
from app.notifications.schemas import (
    NotificationPreferenceResponse,
    NotificationPreferenceUpdate,
    NotificationResponse,
)
from app.notifications.service import NotificationService

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=PaginatedResponse[NotificationResponse])
async def list_notifications(
    service: Annotated[NotificationService, Depends(get_notification_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    is_read: bool | None = Query(None),
    category: str | None = Query(None),
    severity: str | None = Query(None),
):
    items, total = await service.list_notifications(
        workspace_id, user["id"], is_read, category, severity, pagination.limit, pagination.offset
    )
    return PaginatedResponse(
        items=[NotificationResponse.model_validate(i) for i in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get("/unread-count")
async def get_unread_count(
    service: Annotated[NotificationService, Depends(get_notification_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    return {"count": await service.unread_count(workspace_id, user["id"])}


@router.get("/preferences", response_model=list[NotificationPreferenceResponse])
async def list_preferences(
    service: Annotated[NotificationService, Depends(get_notification_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    prefs = await service.get_preferences(workspace_id, user["id"])
    return [NotificationPreferenceResponse.model_validate(p) for p in prefs]


@router.patch(
    "/{notification_id}/read",
    response_model=NotificationResponse,
    dependencies=[Depends(require_min_role("member"))],
)
async def mark_read(
    notification_id: uuid.UUID,
    service: Annotated[NotificationService, Depends(get_notification_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    return NotificationResponse.model_validate(
        await service.mark_read(workspace_id, user["id"], notification_id)
    )


@router.post("/mark-all-read", dependencies=[Depends(require_min_role("member"))])
async def mark_all_read(
    service: Annotated[NotificationService, Depends(get_notification_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    return {"updated": await service.mark_all_read(workspace_id, user["id"])}


@router.delete(
    "/{notification_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_min_role("member"))],
)
async def dismiss(
    notification_id: uuid.UUID,
    service: Annotated[NotificationService, Depends(get_notification_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    await service.dismiss(workspace_id, user["id"], notification_id)


@router.patch(
    "/preferences/{category}",
    response_model=NotificationPreferenceResponse,
    dependencies=[Depends(require_min_role("member"))],
)
async def patch_preference(
    category: str,
    payload: NotificationPreferenceUpdate,
    service: Annotated[NotificationService, Depends(get_notification_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    pref = await service.update_preference(
        workspace_id, user["id"], category, payload.model_dump(exclude_unset=True)
    )
    return NotificationPreferenceResponse.model_validate(pref)
