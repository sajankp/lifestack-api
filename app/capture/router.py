from contextlib import suppress

import structlog
from fastapi import APIRouter, WebSocket

from app.auth.repository import AuthSessionRepository, UserRepository
from app.capture.agent import run_agent_session
from app.core.auth import get_user_info_from_token
from app.core.database import postgres
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.platform.repository import MembershipRepository, WorkspaceRepository
from app.platform.service import WorkspaceService
from app.spending.repository import CategoryRepository
from app.spending.service import CategoryService

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/capture",
    tags=["capture"],
)


async def authenticate_ws(websocket: WebSocket) -> tuple[int, int]:
    token = websocket.cookies.get("access_token")

    if not token:
        raise UnauthorizedError(detail="Missing authorization token")

    async with postgres.async_session_maker() as session:
        auth_session_repo = AuthSessionRepository(session)
        username, user_id_str, sid, default_workspace_id = get_user_info_from_token(token)
        user_id = int(user_id_str)

        auth_session = await auth_session_repo.get_active_by_sid(sid, user_id)
        if not auth_session:
            raise UnauthorizedError(detail="Session is no longer active")

        user_repo = UserRepository(session)
        user = await user_repo.get_by_id(user_id)
        if not user or not user.is_active:
            raise UnauthorizedError(detail="User is inactive")

        membership_repo = MembershipRepository(session)
        workspace_repo = WorkspaceRepository(session)
        workspace_service = WorkspaceService(workspace_repo, membership_repo)

        workspace_id = None
        if default_workspace_id is not None:
            claimed_workspace_id = int(default_workspace_id)
            membership = await membership_repo.get_membership(claimed_workspace_id, user_id)
            if membership:
                workspace_id = claimed_workspace_id

        if workspace_id is None:
            workspaces = await workspace_service.get_user_workspaces(user_id)
            if workspaces:
                workspace_id = workspaces[0].id
            else:
                workspace = await workspace_service.ensure_default_workspace(user_id, username)
                cat_repo = CategoryRepository(session)
                category_service = CategoryService(cat_repo)
                await category_service.provision_default_categories(workspace.id)
                workspace_id = workspace.id
                await session.commit()

        # Enforce that the user has at least "member" role in the workspace.
        final_membership = await membership_repo.get_membership(workspace_id, user_id)
        if not final_membership:
            raise ForbiddenError(detail="Not a member of this workspace")

        user_role = (
            str(final_membership.role.value).lower()
            if hasattr(final_membership.role, "value")
            else str(final_membership.role).split(".")[-1].lower()
        )
        role_rank = {
            "owner": 4,
            "admin": 3,
            "member": 2,
            "viewer": 1,
        }
        if role_rank.get(user_role, 0) < role_rank.get("member", 0):
            raise ForbiddenError(detail="Insufficient workspace permissions")

        return user_id, workspace_id


@router.websocket("/agent/ws")
async def websocket_agent_endpoint(websocket: WebSocket):
    try:
        user_id, workspace_id = await authenticate_ws(websocket)
    except ForbiddenError as e:
        # Authorization (role) rejections are deterministic, so the client must
        # not retry. A pre-accept close never reaches the browser as its own
        # code (the handshake just fails → 1006, which the spec-079 Stage B web
        # client treats as a transient drop and retries five times) — accept
        # first so the browser observes 4003, which is in its no-retry set.
        logger.error("ws_authorization_failed", error=str(e.detail))
        with suppress(Exception):
            await websocket.accept()
            await websocket.close(code=4003)
        return
    except Exception as e:
        logger.error("ws_authentication_failed", error=str(e))
        with suppress(Exception):
            await websocket.close(code=4001)
        return
    await websocket.accept()
    # spec-079 Stage B: a client reconnecting after a dropped session passes the
    # last resumption handle it received so Gemini restores the conversation
    # context (no-op unless CAPTURE_ENABLE_SESSION_RESUMPTION is set).
    # spec-090: it also passes the prior connection's session id
    # (`prev_session`) so the capture log can correlate resumed sessions.
    await run_agent_session(
        websocket,
        user_id,
        workspace_id,
        websocket.query_params.get("timezone", "UTC"),
        (websocket.query_params.get("resume") or "").strip() or None,
        (websocket.query_params.get("prev_session") or "").strip() or None,
    )
