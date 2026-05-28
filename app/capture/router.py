import structlog
from fastapi import APIRouter, WebSocket

from app.auth.repository import AuthSessionRepository
from app.capture.agent import run_agent_session
from app.core.auth import get_user_info_from_token
from app.core.database.postgres import async_session_maker
from app.core.exceptions import UnauthorizedError
from app.platform.repository import MembershipRepository, WorkspaceRepository
from app.platform.service import WorkspaceService
from app.spending.repository import CategoryRepository
from app.spending.service import CategoryService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/capture", tags=["capture"])


async def authenticate_ws(websocket: WebSocket) -> tuple[int, int]:
    token = websocket.cookies.get("access_token")
    if not token:
        token = websocket.query_params.get("token")

    if not token:
        raise UnauthorizedError(detail="Missing authorization token")

    async with async_session_maker() as session:
        auth_session_repo = AuthSessionRepository(session)
        username, user_id_str, sid, default_workspace_id = get_user_info_from_token(token)
        user_id = int(user_id_str)

        auth_session = await auth_session_repo.get_active_by_sid(sid, user_id)
        if not auth_session:
            raise UnauthorizedError(detail="Session is no longer active")

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

        return user_id, workspace_id


@router.websocket("/agent/ws")
async def websocket_agent_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        user_id, workspace_id = await authenticate_ws(websocket)
    except Exception as e:
        logger.error("ws_authentication_failed", error=str(e))
        try:
            await websocket.send_json({
                "type": "error",
                "message": f"Authentication failed: {str(e)}",
            })
            await websocket.close(code=4001)
        except Exception:
            pass
        return
    await run_agent_session(websocket, user_id, workspace_id)
