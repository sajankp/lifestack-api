import contextlib
import uuid
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile, status
from fastapi.responses import PlainTextResponse, Response

from app.config import settings
from app.core.audit import AuditLogger
from app.core.dependencies import (
    get_audit_logger,
    get_current_user,
    get_current_workspace_id,
    get_import_service,
    require_min_role,
)
from app.core.pagination import PaginatedResponse, PaginationParams, build_page
from app.imports.models import ImportModule, ImportStatus
from app.imports.schemas import (
    ImportBatchResponse,
    ImportCommitResponse,
    ImportErrorResponse,
    ImportErrorSummary,
    ImportPreviewRowResponse,
    ImportValidateResponse,
)
from app.imports.service import ImportService, run_background_commit, run_background_validate

router = APIRouter(
    prefix="/imports",
    tags=["imports"],
    dependencies=[Depends(require_min_role("member"))],
)


def _extra_lists(extra_json: dict | None) -> tuple[list, list]:
    extra = extra_json or {}
    return extra.get("skipped", []), extra.get("corporate_action_suspected", [])


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
    response: Response,
    background_tasks: BackgroundTasks,
    module: ImportModule = Form(...),
    file: UploadFile = File(...),
    target_account_id: uuid.UUID | None = Form(None),
    # Demat CAS PDFs are always password-protected (spec-060). Never
    # persisted to ImportBatch or logged — forwarded in-memory only, as a
    # plain function argument, to whichever code path parses the PDF.
    file_password: str | None = Form(None),
    # finance-account-statement only: user-selected date-format identifier
    # (spec-078 owner decision) applied uniformly to the whole file.
    date_format: str | None = Form(None),
    service: ImportService = Depends(get_import_service),
    workspace_id: int = Depends(get_current_workspace_id),
    user: dict = Depends(get_current_user),
    audit_logger: AuditLogger = Depends(get_audit_logger),
):
    batch, temp_path = await service.validate_upload(
        workspace_id, user["id"], module, file, audit_logger, target_account_id, date_format
    )
    if settings.RUN_BACKGROUND_TASKS_SYNCHRONOUSLY:
        try:
            batch, errors = await service.validate_batch_file(
                workspace_id,
                user["id"],
                batch,
                temp_path,
                audit_logger,
                file_password=file_password,
            )
        finally:
            with contextlib.suppress(Exception):
                Path(temp_path).unlink(missing_ok=True)
        response_errors = [ImportErrorResponse.model_validate(e) for e in errors]
        preview_rows = []
        if batch.status == ImportStatus.validated:
            rows = await service.repository.iter_preview_rows_chunk(batch.id, limit=100, offset=0)
            preview_rows = [ImportPreviewRowResponse.model_validate(r) for r in rows]
        skipped, corporate_action_suspected = _extra_lists(batch.extra_json)
        return ImportValidateResponse(
            import_batch=ImportBatchResponse.model_validate(batch),
            errors=response_errors,
            error_summary=_build_error_summary(batch.error_rows, response_errors),
            preview_rows=preview_rows,
            skipped=skipped,
            corporate_action_suspected=corporate_action_suspected,
        )
    else:
        # The batch was flushed but not committed — FastAPI's generator dependency
        # commits AFTER background tasks run, so the worker would query the DB and
        # find nothing. Committing here makes the batch visible to the worker.
        await service.session.commit()
        response.status_code = status.HTTP_202_ACCEPTED
        background_tasks.add_task(
            run_background_validate,
            workspace_id,
            user["id"],
            batch.public_id,
            temp_path,
            file_password,
        )
        return ImportValidateResponse(
            import_batch=ImportBatchResponse.model_validate(batch),
            errors=[],
            error_summary=ImportErrorSummary(
                total_errors=0,
                returned_errors=0,
                by_code={},
                by_field={},
            ),
            preview_rows=[],
        )


@router.post("/{import_public_id}/commit", response_model=ImportCommitResponse)
async def commit_import(
    import_public_id: uuid.UUID,
    response: Response,
    background_tasks: BackgroundTasks,
    service: ImportService = Depends(get_import_service),
    workspace_id: int = Depends(get_current_workspace_id),
    user: dict = Depends(get_current_user),
    audit_logger: AuditLogger = Depends(get_audit_logger),
):
    if settings.RUN_BACKGROUND_TASKS_SYNCHRONOUSLY:
        await service.start_commit(workspace_id, import_public_id)
        batch, inserted, auto_created_categories = await service.commit_batch(
            workspace_id, user["id"], import_public_id, audit_logger
        )
        return ImportCommitResponse(
            import_batch=ImportBatchResponse.model_validate(batch),
            inserted_rows=inserted,
            auto_created_categories=auto_created_categories,
            auto_created_category_count=len(auto_created_categories),
        )
    else:
        batch = await service.start_commit(workspace_id, import_public_id)
        # Commit the status transition (validated → committing) before the worker runs
        # for the same reason as in upload_and_validate: the worker's session won't see
        # the updated status until it is committed.
        await service.session.commit()
        response.status_code = status.HTTP_202_ACCEPTED
        background_tasks.add_task(
            run_background_commit,
            workspace_id,
            user["id"],
            import_public_id,
        )
        return ImportCommitResponse(
            import_batch=ImportBatchResponse.model_validate(batch),
            inserted_rows=0,
            auto_created_categories=[],
            auto_created_category_count=0,
        )


@router.get("", response_model=PaginatedResponse[ImportBatchResponse])
async def list_imports(
    service: ImportService = Depends(get_import_service),
    workspace_id: int = Depends(get_current_workspace_id),
    _user: dict = Depends(get_current_user),
    pagination: PaginationParams = Depends(),
):
    items, total = await service.list_batches(workspace_id, pagination.limit, pagination.offset)
    return build_page([ImportBatchResponse.model_validate(i) for i in items], total, pagination)


@router.get("/{import_public_id}", response_model=ImportValidateResponse)
async def get_import_detail(
    import_public_id: uuid.UUID,
    service: ImportService = Depends(get_import_service),
    workspace_id: int = Depends(get_current_workspace_id),
    _user: dict = Depends(get_current_user),
):
    batch, errors = await service.get_batch_with_errors(workspace_id, import_public_id)
    response_errors = [ImportErrorResponse.model_validate(e) for e in errors]

    preview_rows = []
    if batch.status == ImportStatus.validated:
        rows = await service.repository.iter_preview_rows_chunk(batch.id, limit=100, offset=0)
        preview_rows = [ImportPreviewRowResponse.model_validate(r) for r in rows]

    skipped, corporate_action_suspected = _extra_lists(batch.extra_json)
    return ImportValidateResponse(
        import_batch=ImportBatchResponse.model_validate(batch),
        errors=response_errors,
        error_summary=_build_error_summary(batch.error_rows, response_errors),
        preview_rows=preview_rows,
        skipped=skipped,
        corporate_action_suspected=corporate_action_suspected,
    )


@router.delete("/{import_public_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_import(
    import_public_id: uuid.UUID,
    service: ImportService = Depends(get_import_service),
    workspace_id: int = Depends(get_current_workspace_id),
    user: dict = Depends(get_current_user),
    audit_logger: AuditLogger = Depends(get_audit_logger),
):
    """Delete an import batch and all its associated errors/preview rows.

    Completed spending-transaction imports also roll back their imported records.
    """
    await service.delete_batch(workspace_id, user["id"], import_public_id, audit_logger)
