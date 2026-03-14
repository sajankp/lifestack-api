import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.dependencies import get_current_user, get_current_workspace_id, get_todo_service
from app.todo.schemas import TodoCreate, TodoResponse, TodoUpdate
from app.todo.service import TodoService

router = APIRouter(prefix="/todo", tags=["todo"])


@router.get("/", response_model=list[TodoResponse])
async def list_todos(
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
    completed: bool | None = Query(None),
):
    return await todo_service.list_todos(workspace_id, completed)


@router.post("/", response_model=TodoResponse, status_code=status.HTTP_201_CREATED)
async def create_todo(
    todo_in: TodoCreate,
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    return await todo_service.create_todo(user["id"], workspace_id, todo_in)


@router.get("/{todo_id}", response_model=TodoResponse)
async def get_todo(
    todo_id: uuid.UUID,
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    return await todo_service.get_todo(workspace_id, todo_id)


@router.patch("/{todo_id}", response_model=TodoResponse)
async def update_todo(
    todo_id: uuid.UUID,
    todo_in: TodoUpdate,
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    return await todo_service.update_todo(workspace_id, todo_id, todo_in)


@router.delete("/{todo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_todo(
    todo_id: uuid.UUID,
    todo_service: Annotated[TodoService, Depends(get_todo_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    user: Annotated[dict, Depends(get_current_user)],
):
    await todo_service.delete_todo(workspace_id, todo_id)
