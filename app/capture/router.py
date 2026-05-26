from typing import Annotated

from fastapi import APIRouter, Depends

from app.capture.schemas import CaptureRequest, CaptureResponse
from app.capture.service import CaptureService
from app.core.dependencies import get_capture_service, get_current_user, get_current_workspace_id

router = APIRouter(prefix="/capture", tags=["capture"])


@router.post("", response_model=CaptureResponse)
async def post_capture(
    payload: CaptureRequest,
    service: Annotated[CaptureService, Depends(get_capture_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    result = await service.capture(
        user["id"],
        workspace_id,
        payload.text,
        payload.module,
        payload.hints.model_dump() if payload.hints else None,
    )
    return CaptureResponse.model_validate(result)
