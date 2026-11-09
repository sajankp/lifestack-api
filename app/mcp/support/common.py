"""Helpers shared by more than one MCP tool domain.

Tools call these through the module (``common.run_capture_tool(...)``) so tests patch one
target, ``app.mcp.support.common.<name>``, wherever the calling tool lives.
"""

from datetime import UTC, datetime
from typing import Any

from app.auth.models import User
from app.capture.tools import AgentTools
from app.core.database import postgres
from app.mcp import security


async def run_capture_tool(
    workspace_id: int,
    required_scope: str,
    tool_name: str,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Run a voice-compatible tool behind MCP's workspace and scope checks."""
    async with postgres.async_session_maker() as session:
        user_id = await security.authorize_workspace(
            session,
            workspace_id,
            required_scope=required_scope,
            tool=tool_name,
        )
        user = await session.get(User, user_id)
        agent_tools = AgentTools(
            session=session,
            user_id=user_id,
            workspace_id=workspace_id,
            user_timezone=(user.timezone if user and user.timezone else "UTC"),
            source_channel="mcp_agent",
        )
        result = await getattr(agent_tools, tool_name)(**kwargs)
        if result.get("status") == "success":
            await session.commit()
        else:
            await session.rollback()
        return result


def parse_iso_datetime(value: str | None, label: str) -> datetime | None:
    """ISO-8601 -> aware UTC datetime (naive input is read as UTC)."""
    if value is None or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 date or datetime.") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
