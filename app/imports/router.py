import uuid
from collections import Counter

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
    ImportErrorSummary,
    ImportValidateResponse,
)
from app.imports.service import ImportService

router = APIRouter(prefix="/imports", tags=["imports"])


def _build_error_summary(
    total_errors: int,
    errors: list[ImportErrorResponse],
) -> ImportErrorSummary:
    by_code = Counter(err.error_code for err in errors)
    by_field = Counter(err.field_name for err in errors if err.field_name)
    return ImportErrorSummary(
        total_errors=total_errors,
        returned_errors=len(errors),
        by_code=dict(by_code),
        by_field=dict(by_field),
    )


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
    response_errors = [ImportErrorResponse.model_validate(e) for e in errors]
    return ImportValidateResponse(
        import_batch=ImportBatchResponse.model_validate(batch),
        errors=response_errors,
        error_summary=_build_error_summary(batch.error_rows, response_errors),
    )


@router.post("/{import_public_id}/commit", response_model=ImportCommitResponse)
async def commit_import(
    import_public_id: uuid.UUID,
    service: ImportService = Depends(get_import_service),
    workspace_id: int = Depends(get_current_workspace_id),
    user: dict = Depends(get_current_user),
    audit_logger: AuditLogger = Depends(get_audit_logger),
):
    batch, inserted, auto_created_categories = await service.commit_import(
        workspace_id, user["id"], import_public_id, audit_logger
    )
    return ImportCommitResponse(
        import_batch=ImportBatchResponse.model_validate(batch),
        inserted_rows=inserted,
        auto_created_categories=auto_created_categories,
        auto_created_category_count=len(auto_created_categories),
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
    response_errors = [ImportErrorResponse.model_validate(e) for e in errors]
    return ImportValidateResponse(
        import_batch=ImportBatchResponse.model_validate(batch),
        errors=response_errors,
        error_summary=_build_error_summary(batch.error_rows, response_errors),
    )
