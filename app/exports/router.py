import uuid
from io import BytesIO

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse

from app.core.audit import AuditLogger
from app.core.dependencies import (
    get_audit_logger,
    get_current_user,
    get_current_workspace_id,
    get_export_service,
    require_min_role,
)
from app.core.exceptions import NotFoundError
from app.exports.models import ExportStatus
from app.exports.schemas import ExportCreate, ExportResponse
from app.exports.service import ExportService

router = APIRouter(
    prefix="/exports",
    tags=["exports"],
    dependencies=[Depends(require_min_role("member"))],
)


@router.post("", response_model=ExportResponse, status_code=status.HTTP_201_CREATED)
async def create_export(
    export_in: ExportCreate,
    service: ExportService = Depends(get_export_service),
    workspace_id: int = Depends(get_current_workspace_id),
    user: dict = Depends(get_current_user),
    audit_logger: AuditLogger = Depends(get_audit_logger),
):
    record = await service.create_export(
        workspace_id=workspace_id,
        requested_by=user["id"],
        export_in=export_in,
        audit_logger=audit_logger,
    )
    return ExportResponse.model_validate(record)


@router.get("/{export_public_id}", response_model=ExportResponse)
async def get_export(
    export_public_id: uuid.UUID,
    service: ExportService = Depends(get_export_service),
    workspace_id: int = Depends(get_current_workspace_id),
    _user: dict = Depends(get_current_user),
):
    record = await service.get_export(workspace_id, export_public_id)
    return ExportResponse.model_validate(record)


@router.get("/{export_public_id}/download")
async def download_export(
    export_public_id: uuid.UUID,
    service: ExportService = Depends(get_export_service),
    workspace_id: int = Depends(get_current_workspace_id),
    _user: dict = Depends(get_current_user),
):
    record = await service.get_export(workspace_id, export_public_id, include_blob=True)
    if record.artifact_blob is None or record.status != ExportStatus.ready:
        raise NotFoundError(detail="Export artifact is not available")

    headers = {
        "Content-Disposition": f'attachment; filename="{record.artifact_filename or "export.bin"}"'
    }
    return StreamingResponse(
        BytesIO(record.artifact_blob),
        media_type=record.artifact_mime_type or "application/octet-stream",
        headers=headers,
    )


@router.delete("/{export_public_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_export(
    export_public_id: uuid.UUID,
    service: ExportService = Depends(get_export_service),
    workspace_id: int = Depends(get_current_workspace_id),
    _user: dict = Depends(get_current_user),
):
    """Delete an export record (completed or failed exports only).

    Exports that are still pending cannot be deleted.
    """
    await service.delete_export(workspace_id, export_public_id)
