"""Prometheus metrics for HTTP requests and voice tools (spec-110).

Everything here is in-process and bounded: the production VM has ~270 MB of
free memory, so label sets are kept small on purpose.
"""

from collections.abc import Mapping
from typing import Any

from prometheus_client import Counter, Histogram

from app.config import settings

_STANDARD_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
UNMATCHED_ROUTE = "unmatched"
# Requests under the MCP mount carry no route template (the MCP app is mounted at "/").
MCP_ROUTE = "mcp"
# Label for work done outside any HTTP request (scheduler jobs, CLI).
NO_ROUTE = "none"
# Probes and the scrape itself would dominate the series and say nothing useful.
_EXCLUDED_ROUTES = frozenset({"/health", "/metrics"})

http_request_duration_seconds = Histogram(
    "lifestack_http_request_duration_seconds",
    "HTTP request duration by route template",
    ["method", "route", "status_class"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

db_slow_queries_total = Counter(
    "lifestack_db_slow_queries_total",
    "SQL statements slower than SLOW_QUERY_THRESHOLD_MS",
    ["route"],
)

# spec-112: where a request's time goes. Queries per request exposes N+1 loops; DB seconds next to
# the request duration separates database time from everything else (outbound calls, CPU, swap).
http_request_db_queries = Histogram(
    "lifestack_http_request_db_queries",
    "SQL statements executed per HTTP request, by route template",
    ["route"],
    buckets=[0, 1, 2, 5, 10, 20, 50, 100, 200, 500],
)
http_request_db_seconds = Histogram(
    "lifestack_http_request_db_seconds",
    "Time spent executing SQL per HTTP request, by route template",
    ["route"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

mcp_tool_duration_seconds = Histogram(
    "lifestack_mcp_tool_duration_seconds",
    "MCP tool call duration",
    ["tool", "status"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

capture_tool_duration_seconds = Histogram(
    "lifestack_capture_tool_duration_seconds",
    "Voice-agent tool execution time",
    ["tool", "status"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)


def route_for_scope(scope: Mapping[str, Any]) -> str:
    """Route template for an ASGI scope. Starlette mutates the scope in place while routing,
    so this is valid for the whole request, including from the DB layer."""
    path = getattr(scope.get("route"), "path", None)
    if isinstance(path, str) and path:
        return path
    mount = settings.MCP_MOUNT_PATH.rstrip("/")
    request_path = scope.get("path", "")
    if mount and (request_path == mount or request_path.startswith(mount + "/")):
        return MCP_ROUTE  # a prefix match, not a substring: "/mcpx" stays unmatched
    return UNMATCHED_ROUTE


def route_label(request) -> tuple[str, str]:
    """(method, route-template) for a request. The template comes from the matched
    route, never the raw path, so ids and scanner noise cannot mint new series."""
    method = request.method if request.method in _STANDARD_METHODS else "OTHER"
    return method, route_for_scope(request.scope)


def observe_request(request, status_code: int, duration_seconds: float) -> str:
    """Record one finished request; returns the route label (for the log line)."""
    method, route = route_label(request)
    if route not in _EXCLUDED_ROUTES:
        http_request_duration_seconds.labels(
            method=method, route=route, status_class=f"{status_code // 100}xx"
        ).observe(duration_seconds)
    return route


def observe_request_db(route: str, profile) -> None:
    """Record a finished request's query count and DB time (same exclusions as the latency
    histogram: probes and the scrape itself would only add noise)."""
    if profile is None or route in _EXCLUDED_ROUTES:
        return
    http_request_db_queries.labels(route=route).observe(profile.queries)
    http_request_db_seconds.labels(route=route).observe(profile.seconds)


def observe_mcp_tool(
    tool: str, status: str, duration_seconds: float, *, known_tools: frozenset[str]
) -> None:
    """Record one MCP tool call. Names come from the client, so anything the server did not
    register is folded into `other` to keep labels bounded."""
    label = tool if tool in known_tools else "other"
    mcp_tool_duration_seconds.labels(tool=label, status=status).observe(duration_seconds)


def observe_capture_tool(
    tool: str, status: str, duration_seconds: float, *, known_tools: frozenset[str]
) -> None:
    """Record one executed voice tool. Tool names come from the model, so anything
    outside `known_tools` (the declared tool set, supplied by the caller so this
    module stays independent of the capture package) is folded into `other` to
    keep labels bounded."""
    label = tool if tool in known_tools else "other"
    capture_tool_duration_seconds.labels(tool=label, status=status or "unknown").observe(
        duration_seconds
    )
