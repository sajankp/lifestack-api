import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import select

from app.auth.models import User
from app.config import settings
from app.core.auth import create_token
from app.core.csrf import issue_csrf_token
from app.core.dependencies import (
    get_current_user,
    get_db_session,
    get_membership_repo,
    get_workspace_repo,
    get_workspace_service,
)
from app.core.exceptions import ForbiddenError, NotFoundError
from app.platform.models import WorkspaceMembership, WorkspaceRole
from app.platform.repository import MembershipRepository, WorkspaceRepository
from app.platform.service import WorkspaceService

router = APIRouter(prefix="/platform", tags=["platform"])


class WorkspaceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    public_id: uuid.UUID
    name: str
    description: str | None = None
    is_active: bool
    role: str | None = None


class WorkspaceListResponse(BaseModel):
    items: list[WorkspaceResponse]


class WorkspaceMemberAdd(BaseModel):
    user_public_id: uuid.UUID
    role: WorkspaceRole


def _workspace_role_value(role: WorkspaceRole | str | None) -> str | None:
    if role is None:
        return None
    if isinstance(role, WorkspaceRole):
        return role.value
    return role


@router.get("/workspaces/", response_model=WorkspaceListResponse)
async def list_workspaces(
    current_user: Annotated[dict, Depends(get_current_user)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
    membership_repo: Annotated[MembershipRepository, Depends(get_membership_repo)],
):
    workspaces = await workspace_service.get_user_workspaces(current_user["id"])
    memberships = await membership_repo.list_user_memberships(current_user["id"])
    membership_by_workspace_id = {membership.workspace_id: membership for membership in memberships}
    items = []
    for w in workspaces:
        membership = membership_by_workspace_id.get(w.id)
        items.append(
            WorkspaceResponse(
                public_id=w.public_id,
                name=w.name,
                description=w.description,
                is_active=w.is_active,
                role=_workspace_role_value(membership.role if membership else None),
            )
        )
    return WorkspaceListResponse(items=items)


@router.post("/workspaces/{workspace_id}/members", status_code=status.HTTP_201_CREATED)
async def add_workspace_member(
    workspace_id: uuid.UUID,
    member_add: WorkspaceMemberAdd,
    current_user: Annotated[dict, Depends(get_current_user)],
    workspace_repo: Annotated[WorkspaceRepository, Depends(get_workspace_repo)],
    membership_repo: Annotated[MembershipRepository, Depends(get_membership_repo)],
    session=Depends(get_db_session),
):
    # 1. Resolve target workspace
    workspace = await workspace_repo.get_by_public_id(workspace_id)
    if not workspace or not workspace.is_active:
        raise NotFoundError(detail="Workspace not found or is inactive")

    # 2. Check if current user is owner or admin of the workspace (mutations require admin/owner)
    current_membership = await membership_repo.get_membership(workspace.id, current_user["id"])
    if not current_membership or current_membership.role not in [
        WorkspaceRole.OWNER,
        WorkspaceRole.ADMIN,
    ]:
        raise ForbiddenError(detail="Insufficient workspace permissions to invite members")

    # 3. Resolve target user to invite
    stmt = select(User).where(User.public_id == member_add.user_public_id)
    result = await session.execute(stmt)
    target_user = result.scalar_one_or_none()
    if not target_user or not target_user.is_active:
        raise NotFoundError(detail="User not found or is inactive")

    # 4. Check if membership already exists
    existing = await membership_repo.get_membership(workspace.id, target_user.id)
    if existing:
        return {"status": "already_member"}

    # 5. Create membership
    membership = WorkspaceMembership(
        workspace_id=workspace.id,
        user_id=target_user.id,
        role=member_add.role,
    )
    await membership_repo.create(membership)
    await session.commit()
    return {"status": "invited"}


@router.post("/workspaces/{workspace_id}/select", status_code=status.HTTP_204_NO_CONTENT)
async def select_workspace(
    workspace_id: uuid.UUID,
    response: Response,
    current_user: Annotated[dict, Depends(get_current_user)],
    workspace_repo: Annotated[WorkspaceRepository, Depends(get_workspace_repo)],
    membership_repo: Annotated[MembershipRepository, Depends(get_membership_repo)],
):
    # 1. Resolve workspace
    workspace = await workspace_repo.get_by_public_id(workspace_id)
    if not workspace or not workspace.is_active:
        raise NotFoundError(detail="Workspace not found or is inactive")

    # 2. Verify membership
    membership = await membership_repo.get_membership(workspace.id, current_user["id"])
    if not membership:
        raise ForbiddenError(detail="Not a member of this workspace")

    # 3. Issue new tokens with updated default_workspace_id claim
    access_token_expires = timedelta(seconds=settings.ACCESS_TOKEN_EXPIRE_SECONDS)
    access_token = create_token(
        data={
            "sub": current_user["username"],
            "sub_id": str(current_user["id"]),
            "default_workspace_id": workspace.id,
        },
        expires_delta=access_token_expires,
        sid=current_user["sid"],
        token_type="access",
    )

    refresh_token_expires = timedelta(seconds=settings.REFRESH_TOKEN_EXPIRE_SECONDS)
    refresh_token = create_token(
        data={
            "sub": current_user["username"],
            "sub_id": str(current_user["id"]),
            "default_workspace_id": workspace.id,
        },
        expires_delta=refresh_token_expires,
        sid=current_user["sid"],
        token_type="refresh",
    )

    # 4. Set HttpOnly cookies
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_SECONDS,
        path="/",
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        secure=settings.COOKIE_SECURE,
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        max_age=settings.REFRESH_TOKEN_EXPIRE_SECONDS,
        path="/",
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        secure=settings.COOKIE_SECURE,
    )
    issue_csrf_token(response, max_age=settings.REFRESH_TOKEN_EXPIRE_SECONDS)
