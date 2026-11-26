"""Per-tool timing for MCP tool calls (spec-112).

The MCP app is mounted at `/`, so its requests have no FastAPI route template and used to be
lumped into `route="unmatched"` (~92 % of production traffic). This middleware times each tool
call by tool name instead. Tool names are chosen by the client, so anything not registered on the
server is folded into `other` to keep the label set bounded.
"""

import time
from collections.abc import Awaitable, Callable, Iterable

import structlog
from fastmcp.server.middleware import Middleware

from app.observability.request_metrics import observe_mcp_tool

logger = structlog.get_logger()


class McpToolMetricsMiddleware(Middleware):
    def __init__(self, known_tool_names: Callable[[], Awaitable[Iterable[str]]]) -> None:
        self._provider = known_tool_names
        self._known: frozenset[str] | None = None

    async def _names(self) -> frozenset[str]:
        if self._known is None:
            try:
                self._known = frozenset(await self._provider())
            except Exception:
                # Never break a tool call over a metrics label; retry on the next call.
                logger.warning("mcp_tool_names_unavailable", exc_info=True)
                return frozenset()
        return self._known

    async def on_call_tool(self, context, call_next):
        started = time.perf_counter()
        status = "success"
        try:
            return await call_next(context)
        except Exception:
            status = "error"
            raise
        finally:
            observe_mcp_tool(
                context.message.name,
                status,
                time.perf_counter() - started,
                known_tools=await self._names(),
            )
