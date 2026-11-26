"""Authenticated MCP tools and resources for Lifestack."""

from urllib.parse import urlparse

from fastmcp import FastMCP

from app.config import settings
from app.mcp.auth import LifestackTokenVerifier
from app.mcp.tools import (
    finance,
    health,
    health_measurements,
    orders,
    portfolio,
    research,
    spending,
    tax,
    todos,
    workspace,
)
from app.observability.mcp_metrics import McpToolMetricsMiddleware

_TOOL_MODULES = (
    todos,
    workspace,
    finance,
    tax,
    spending,
    health,
    health_measurements,
    portfolio,
    research,
    orders,
)


def create_mcp_server() -> FastMCP:
    """Build the Lifestack MCP server: auth, then every domain registers its tools."""
    mcp = FastMCP("Lifestack", auth=LifestackTokenVerifier())
    for module in _TOOL_MODULES:
        module.register(mcp)
    # spec-112: time every tool call by (bounded) tool name; see observability/mcp_metrics.py.
    mcp.add_middleware(McpToolMetricsMiddleware(lambda: registered_tool_names(mcp)))
    return mcp


async def registered_tool_names(server: FastMCP) -> set[str]:
    """Names of the tools this server registered (the bound for the MCP metric label)."""
    return {tool.name for tool in await server.list_tools()}


def create_mcp_asgi_app(mcp: FastMCP, path: str = "/mcp"):
    """Build the authenticated Streamable HTTP app with DNS-rebinding protection."""
    base_url = urlparse(settings.MCP_BASE_URL or "")
    allowed_hosts = [
        item.strip() for item in settings.MCP_ALLOWED_HOSTS.split(",") if item.strip()
    ] or ([base_url.netloc] if base_url.netloc else [])
    default_origin = (
        f"{base_url.scheme}://{base_url.netloc}" if base_url.scheme and base_url.netloc else None
    )
    configured_origins = [
        settings._normalize_origin(item.strip())
        for item in settings.MCP_ALLOWED_ORIGINS.split(",")
        if item.strip()
    ]
    # MCP has its own transport-level Origin policy. Keep it independent from
    # the API CORS/CSRF settings so MCP clients can be scoped separately.
    # Add browser-based clients (for example, MCP Inspector) explicitly.
    allowed_origins = configured_origins or ([default_origin] if default_origin else [])
    return mcp.http_app(
        path=path,
        host_origin_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
