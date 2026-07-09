import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status

from app.core.audit import AuditLogger
from app.core.dependencies import (
    get_audit_logger,
    get_current_user,
    get_current_workspace_id,
    get_todo_service,
    require_min_role,
)
from app.core.pagination import PaginatedResponse, PaginationParams, build_page
from app.todo.schemas import (
    DeleteCompletedResponse,
    RecurringTodoRuleCreate,
    RecurringTodoRuleResponse,
    RecurringTodoRuleUpdate,
    TodoCreate,
    TodoResponse,
    TodoUpdate,
)
from app.todo.service import TodoService

router = APIRouter(prefix="/todo", tags=["todo"])


@router.get("/", response_model=PaginatedResponse[TodoResponse])
async def list_todos(
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    completed: bool | None = Query(None),
    sort: Literal["created_at", "due_date"] = Query("created_at"),
):
    items, total = await todo_service.list_todos(
        workspace_id, completed, pagination.limit, pagination.offset, sort
    )
    return build_page(items, total, pagination)


@router.post("/", response_model=TodoResponse, status_code=status.HTTP_201_CREATED)
async def create_todo(
    todo_in: TodoCreate,
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    todo = await todo_service.create_todo(
        user["id"], workspace_id, todo_in, audit_logger=audit_logger
    )
    return await todo_service.to_response(workspace_id, todo)


@router.delete("/completed", response_model=DeleteCompletedResponse)
async def delete_completed_todos(
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    deleted = await todo_service.delete_completed_todos(
        workspace_id, actor_id=user["id"], audit_logger=audit_logger
    )
    return DeleteCompletedResponse(deleted=deleted)


@router.get("/recurring/", response_model=PaginatedResponse[RecurringTodoRuleResponse])
async def list_recurring_todos(
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    is_active: bool | None = Query(True),
):
    items, total = await todo_service.list_recurring_rules(
        workspace_id, is_active, pagination.limit, pagination.offset
    )
    return build_page(items, total, pagination)


@router.post(
    "/recurring/", response_model=RecurringTodoRuleResponse, status_code=status.HTTP_201_CREATED
)
async def create_recurring_todo(
    rule_in: RecurringTodoRuleCreate,
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    return await todo_service.create_recurring_rule(
        user["id"], workspace_id, rule_in, audit_logger=audit_logger
    )


@router.patch("/recurring/{rule_id}", response_model=RecurringTodoRuleResponse)
async def update_recurring_todo(
    rule_id: uuid.UUID,
    rule_in: RecurringTodoRuleUpdate,
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    return await todo_service.update_recurring_rule(
        workspace_id, rule_id, rule_in, actor_id=user["id"], audit_logger=audit_logger
    )


@router.delete("/recurring/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_recurring_todo(
    rule_id: uuid.UUID,
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await todo_service.delete_recurring_rule(
        workspace_id, rule_id, actor_id=user["id"], audit_logger=audit_logger
    )


@router.get("/{todo_id}", response_model=TodoResponse)
async def get_todo(
    todo_id: uuid.UUID,
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    return await todo_service.get_todo_response(workspace_id, todo_id)


@router.patch("/{todo_id}", response_model=TodoResponse)
async def update_todo(
    todo_id: uuid.UUID,
    todo_in: TodoUpdate,
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    todo = await todo_service.update_todo(
        workspace_id,
        todo_id,
        todo_in,
        actor_id=user["id"],
        audit_logger=audit_logger,
    )
    return await todo_service.to_response(workspace_id, todo)


@router.delete("/{todo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_todo(
    todo_id: uuid.UUID,
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    await todo_service.delete_todo(
        workspace_id, todo_id, actor_id=user["id"], audit_logger=audit_logger
    )
