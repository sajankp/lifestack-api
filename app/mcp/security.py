"""Tool-level MCP authorization and audit helpers."""

import structlog
from fastmcp.server.dependencies import get_access_token
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import ROLE_RANK, _role_key
from app.core.exceptions import ForbiddenError, UnauthorizedError, ValidationError
from app.platform.repository import MembershipRepository

logger = structlog.get_logger(__name__)

# Scopes that authorize mutating operations: these must additionally satisfy
# the workspace role policy (member or above), mirroring require_min_role
# ("member") on REST mutation endpoints. Read scopes stay membership-gated.
_WRITE_SCOPES = {"mcp:write"}


def principal() -> tuple[int, str]:
    token = get_access_token()
    if token is None:
        raise UnauthorizedError(detail="MCP authentication required")
    try:
        user_id = int(token.claims["user_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UnauthorizedError(detail="MCP token is missing a user identity") from exc
    return user_id, token.client_id


async def authorize_workspace(
    session: AsyncSession, workspace_id: int, *, required_scope: str, tool: str
) -> int:
    if workspace_id <= 0:
        raise ValidationError(detail="workspace_id must be a positive integer")
    token = get_access_token()
    if token is None or required_scope not in token.scopes:
        raise ForbiddenError(detail="MCP token does not grant this operation")
    user_id, _ = principal()
    membership = await MembershipRepository(session).get_membership(workspace_id, user_id)
    if membership is None:
        raise ForbiddenError(detail="You do not have access to this workspace")
    if required_scope in _WRITE_SCOPES and ROLE_RANK.get(
        _role_key(membership.role), 0
    ) < ROLE_RANK.get("member", 0):
        raise ForbiddenError(detail="Insufficient workspace role for MCP write operations")
    logger.info("mcp_tool_authorized", tool=tool, user_id=user_id, workspace_id=workspace_id)
    return user_id


def authorize_user(*, required_scope: str, tool: str) -> int:
    """Authorize an MCP operation that is not scoped to one workspace yet."""
    token = get_access_token()
    if token is None or required_scope not in token.scopes:
        raise ForbiddenError(detail="MCP token does not grant this operation")
    user_id, _ = principal()
    logger.info("mcp_tool_authorized", tool=tool, user_id=user_id)
    return user_id


def validate_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= 100:
        raise ValidationError(detail="limit must be between 1 and 100")
    if offset < 0:
        raise ValidationError(detail="offset must be non-negative")
