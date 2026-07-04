import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.config import settings
from app.core.dependencies import (
    get_current_user,
    get_current_workspace_id,
    get_notification_service,
    get_push_subscription_service,
    require_min_role,
)
from app.core.exceptions import PushNotConfiguredError
from app.core.pagination import PaginatedResponse, PaginationParams, build_page
from app.notifications.schemas import (
    NotificationPreferenceResponse,
    NotificationPreferenceUpdate,
    NotificationResponse,
    PushSubscriptionCreate,
    PushSubscriptionResponse,
)
from app.notifications.service import NotificationService, PushSubscriptionService

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _endpoint_hint(endpoint: str) -> str:
    """Never return the full capability URL to the client — just enough to
    tell subscriptions apart in a settings list."""
    return f"...{endpoint[-24:]}" if len(endpoint) > 24 else endpoint


def _to_subscription_response(subscription) -> PushSubscriptionResponse:
    return PushSubscriptionResponse(
        public_id=subscription.public_id,
        endpoint_hint=_endpoint_hint(subscription.endpoint),
        device_label=subscription.device_label,
        is_active=subscription.is_active,
        last_success_at=subscription.last_success_at,
        last_failure_at=subscription.last_failure_at,
        created_at=subscription.created_at,
    )


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
    return build_page([NotificationResponse.model_validate(i) for i in items], total, pagination)


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


@router.get("/push/vapid-public-key")
async def get_vapid_public_key():
    if not settings.VAPID_PUBLIC_KEY:
        raise PushNotConfiguredError(detail="Push notifications are not configured")
    return {"key": settings.VAPID_PUBLIC_KEY}


@router.post(
    "/push-subscriptions",
    response_model=PushSubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_min_role("member"))],
)
async def create_push_subscription(
    payload: PushSubscriptionCreate,
    service: Annotated[PushSubscriptionService, Depends(get_push_subscription_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    if not settings.VAPID_PUBLIC_KEY:
        raise PushNotConfiguredError(detail="Push notifications are not configured")
    subscription = await service.subscribe(
        workspace_id,
        user["id"],
        payload.endpoint,
        payload.keys.p256dh,
        payload.keys.auth,
        payload.device_label,
    )
    return _to_subscription_response(subscription)


@router.get("/push-subscriptions", response_model=list[PushSubscriptionResponse])
async def list_push_subscriptions(
    service: Annotated[PushSubscriptionService, Depends(get_push_subscription_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    subscriptions = await service.list_subscriptions(workspace_id, user["id"])
    return [_to_subscription_response(s) for s in subscriptions]


@router.delete(
    "/push-subscriptions/{subscription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_min_role("member"))],
)
async def delete_push_subscription(
    subscription_id: uuid.UUID,
    service: Annotated[PushSubscriptionService, Depends(get_push_subscription_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    await service.unsubscribe(workspace_id, user["id"], subscription_id)
