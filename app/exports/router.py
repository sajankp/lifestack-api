import asyncio
import uuid
from io import BytesIO

from fastapi import APIRouter, Depends, status
from fastapi.responses import FileResponse, StreamingResponse

from app.core.audit import AuditLogger
from app.core.dependencies import (
    get_audit_logger,
    get_current_user,
    get_current_workspace_id,
    get_export_service,
    require_min_role,
)
from app.core.exceptions import NotFoundError
from app.exports.schemas import ExportCreate, ExportResponse
from app.exports.service import ExportService

# RBAC is applied per-endpoint (matching every other router) rather than at
# the router prefix (G5). Kept at `member` on the read endpoints too — the
# prefix dependency previously required `member` for reads as well, so
# endpoint-level checks preserve that exact access (viewers still can't read
# exports); loosening reads to any authenticated user would be a behavior
# change, out of scope for a refactor.
router = APIRouter(
    prefix="/exports",
    tags=["exports"],
)


@router.post("", response_model=ExportResponse, status_code=status.HTTP_201_CREATED)
async def create_export(
    export_in: ExportCreate,
    service: ExportService = Depends(get_export_service),
    workspace_id: int = Depends(get_current_workspace_id),
    user: dict = Depends(get_current_user),
    audit_logger: AuditLogger = Depends(get_audit_logger),
    _role: object = Depends(require_min_role("member")),
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
    _role: object = Depends(require_min_role("member")),
):
    record = await service.get_export(workspace_id, export_public_id)
    return ExportResponse.model_validate(record)


@router.get("/{export_public_id}/download")
async def download_export(
    export_public_id: uuid.UUID,
    service: ExportService = Depends(get_export_service),
    workspace_id: int = Depends(get_current_workspace_id),
    _user: dict = Depends(get_current_user),
    _role: object = Depends(require_min_role("member")),
):
    backend, mime_type, filename, data = await service.get_export_download(
        workspace_id, export_public_id
    )

    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}

    if backend == "db":
        return StreamingResponse(
            BytesIO(data),
            media_type=mime_type,
            headers=headers,
        )
    elif backend == "local":
        return FileResponse(
            data,
            media_type=mime_type,
            filename=filename,
        )
    elif backend == "s3":

        async def iter_s3_chunks():
            try:
                while True:
                    chunk = await asyncio.to_thread(data.read, 1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await asyncio.to_thread(data.close)

        return StreamingResponse(
            iter_s3_chunks(),
            media_type=mime_type,
            headers=headers,
        )
    else:
        raise NotFoundError(detail="Unsupported storage backend")


@router.delete("/{export_public_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_export(
    export_public_id: uuid.UUID,
    service: ExportService = Depends(get_export_service),
    workspace_id: int = Depends(get_current_workspace_id),
    _user: dict = Depends(get_current_user),
    _role: object = Depends(require_min_role("member")),
):
    """Delete an export record (completed or failed exports only).

    Exports that are still pending cannot be deleted.
    """
    await service.delete_export(workspace_id, export_public_id)
