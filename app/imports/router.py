import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import PlainTextResponse, Response

from app.core.audit import AuditLogger
from app.core.dependencies import (
    get_audit_logger,
    get_current_user,
    get_current_workspace_id,
    get_import_service,
)
from app.core.pagination import PaginatedResponse, PaginationParams
from app.imports.models import ImportModule
from app.imports.schemas import (
    ImportBatchResponse,
    ImportCommitResponse,
    ImportErrorResponse,
    ImportValidateResponse,
)
from app.imports.service import ImportService

router = APIRouter(prefix="/imports", tags=["imports"])


@router.get("/templates/{module}", response_class=PlainTextResponse)
async def download_template(
    module: ImportModule,
    service: ImportService = Depends(get_import_service),
    _workspace_id: int = Depends(get_current_workspace_id),
    _user: dict = Depends(get_current_user),
):
    return Response(
        content=service.template_csv(module),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module.value}-template.csv"'},
    )


@router.post("", response_model=ImportValidateResponse)
async def upload_and_validate(
    module: ImportModule = Form(...),
    file: UploadFile = File(...),
    service: ImportService = Depends(get_import_service),
    workspace_id: int = Depends(get_current_workspace_id),
    user: dict = Depends(get_current_user),
    audit_logger: AuditLogger = Depends(get_audit_logger),
):
    batch, errors = await service.validate_upload(
        workspace_id, user["id"], module, file, audit_logger
    )
    return ImportValidateResponse(
        import_batch=ImportBatchResponse.model_validate(batch),
        errors=[ImportErrorResponse.model_validate(e) for e in errors],
    )


@router.post("/{import_public_id}/commit", response_model=ImportCommitResponse)
async def commit_import(
    import_public_id: uuid.UUID,
    service: ImportService = Depends(get_import_service),
    workspace_id: int = Depends(get_current_workspace_id),
    user: dict = Depends(get_current_user),
    audit_logger: AuditLogger = Depends(get_audit_logger),
):
    batch, inserted = await service.commit_import(
        workspace_id, user["id"], import_public_id, audit_logger
    )
    return ImportCommitResponse(
        import_batch=ImportBatchResponse.model_validate(batch),
        inserted_rows=inserted,
    )


@router.get("", response_model=PaginatedResponse[ImportBatchResponse])
async def list_imports(
    service: ImportService = Depends(get_import_service),
    workspace_id: int = Depends(get_current_workspace_id),
    _user: dict = Depends(get_current_user),
    pagination: PaginationParams = Depends(),
):
    items, total = await service.list_batches(workspace_id, pagination.limit, pagination.offset)
    return PaginatedResponse(
        items=[ImportBatchResponse.model_validate(i) for i in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get("/{import_public_id}", response_model=ImportValidateResponse)
async def get_import_detail(
    import_public_id: uuid.UUID,
    service: ImportService = Depends(get_import_service),
    workspace_id: int = Depends(get_current_workspace_id),
    _user: dict = Depends(get_current_user),
):
    batch, errors = await service.get_batch_with_errors(workspace_id, import_public_id)
    return ImportValidateResponse(
        import_batch=ImportBatchResponse.model_validate(batch),
        errors=[ImportErrorResponse.model_validate(e) for e in errors],
    )
