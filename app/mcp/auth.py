"""Authentication and authorization helpers for the MCP transport."""

from fastmcp.server.auth import AccessToken, TokenVerifier

from app.auth.repository import AuthSessionRepository, UserRepository
from app.core.auth import get_user_info_from_token
from app.core.database import postgres


class LifestackTokenVerifier(TokenVerifier):
    """Verify Lifestack access JWTs and their revocable server-side sessions."""

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            _username, raw_user_id, sid, default_workspace_id = get_user_info_from_token(token)
            user_id = int(raw_user_id)
        except Exception:
            return None

        async with postgres.async_session_maker() as session:
            auth_session = await AuthSessionRepository(session).get_active_by_sid(sid, user_id)
            user = await UserRepository(session).get_by_id(user_id)
            if auth_session is None or user is None or not user.is_active:
                return None

        return AccessToken(
            token=token,
            client_id=str(user_id),
            subject=str(user_id),
            scopes=["mcp:read", "mcp:write"],
            claims={
                "user_id": user_id,
                "sid": sid,
                "default_workspace_id": default_workspace_id,
            },
        )
