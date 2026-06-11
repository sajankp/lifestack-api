import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.auth.models import User
from app.auth.repository import AuthSessionRepository
from app.config import settings
from app.core.audit import AuditLogger
from app.core.auth import create_token
from app.core.csrf import issue_csrf_token
from app.core.dependencies import (
    get_audit_logger,
    get_auth_session_repo,
    get_current_user,
    get_db_session,
    get_membership_repo,
    get_workspace_repo,
    get_workspace_service,
)
from app.core.exceptions import ForbiddenError, NotFoundError, UnauthorizedError
from app.platform.demo_reset import DemoResetService
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


class DemoResetStatusResponse(BaseModel):
    enabled: bool
    allowed: bool
    workspace_public_id: uuid.UUID
    workspace_name: str
    role: str | None
    reason: str | None = None


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
    auth_session_repo: Annotated[AuthSessionRepository, Depends(get_auth_session_repo)],
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
    auth_session = await auth_session_repo.get_active_by_sid(
        current_user["sid"], current_user["id"]
    )
    if auth_session is None:
        raise UnauthorizedError(detail="Session is no longer active")
    now = datetime.now(UTC)
    auth_session.previous_token_hash = auth_session.current_token_hash
    auth_session.current_token_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
    auth_session.rotated_at = now
    auth_session.last_seen_at = now
    auth_session.expires_at = now + refresh_token_expires
    auth_session_repo.session.add(auth_session)
    await auth_session_repo.session.commit()

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


@router.get(
    "/workspaces/{workspace_id}/reset-demo/status",
    response_model=DemoResetStatusResponse,
)
async def get_demo_reset_status(
    workspace_id: uuid.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    workspace_repo: Annotated[WorkspaceRepository, Depends(get_workspace_repo)],
    membership_repo: Annotated[MembershipRepository, Depends(get_membership_repo)],
):
    workspace = await workspace_repo.get_by_public_id(workspace_id)
    if not workspace or not workspace.is_active:
        raise NotFoundError(detail="Workspace not found or is inactive")

    membership = await membership_repo.get_membership(workspace.id, current_user["id"])
    role = _workspace_role_value(membership.role if membership else None)
    is_active_workspace = current_user.get("default_workspace_id") == workspace.id
    has_role = role in ("owner", "admin")
    allowed = bool(settings.ENABLE_DEMO_RESET and is_active_workspace and has_role)

    reason: str | None = None
    if not settings.ENABLE_DEMO_RESET:
        reason = "feature_flag_disabled"
    elif not is_active_workspace:
        reason = "workspace_not_active"
    elif not membership:
        reason = "not_workspace_member"
    elif not has_role:
        reason = "insufficient_role"

    return DemoResetStatusResponse(
        enabled=settings.ENABLE_DEMO_RESET,
        allowed=allowed,
        workspace_public_id=workspace.public_id,
        workspace_name=workspace.name,
        role=role,
        reason=reason,
    )


@router.post("/workspaces/{workspace_id}/reset-demo", status_code=status.HTTP_200_OK)
async def reset_demo_data(
    workspace_id: uuid.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    workspace_repo: Annotated[WorkspaceRepository, Depends(get_workspace_repo)],
    membership_repo: Annotated[MembershipRepository, Depends(get_membership_repo)],
    audit_logger: Annotated[AuditLogger, Depends(get_audit_logger)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    workspace = await workspace_repo.get_by_public_id(workspace_id)
    if not workspace or not workspace.is_active:
        raise NotFoundError(detail="Workspace not found or is inactive")

    reset_service = DemoResetService(session, audit_logger)

    if current_user.get("default_workspace_id") != workspace.id:
        await reset_service.log_denied(
            workspace=workspace,
            actor_id=current_user["id"],
            reason="workspace_not_active",
        )
        await session.commit()
        raise ForbiddenError(detail="Demo reset is only available for the active workspace")

    membership = await membership_repo.get_membership(workspace.id, current_user["id"])
    if not membership:
        await reset_service.log_denied(
            workspace=workspace,
            actor_id=current_user["id"],
            reason="not_workspace_member",
        )
        await session.commit()
        raise ForbiddenError(detail="Not a member of this workspace")
    if membership.role not in ("owner", "admin"):
        await reset_service.log_denied(
            workspace=workspace,
            actor_id=current_user["id"],
            reason=f"insufficient_role:{_workspace_role_value(membership.role)}",
        )
        await session.commit()
        raise ForbiddenError(detail="Only workspace owners or admins can reset demo data")
    if not settings.ENABLE_DEMO_RESET:
        await reset_service.log_denied(
            workspace=workspace,
            actor_id=current_user["id"],
            reason="feature_flag_disabled",
        )
        await session.commit()
        raise ForbiddenError(detail="Demo reset is disabled in this environment")

    await reset_service.reset_workspace(workspace=workspace, actor_id=current_user["id"])
    await session.commit()
    return {"status": "reset_success"}
