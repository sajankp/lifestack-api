"""Prometheus metrics for HTTP requests and voice tools (spec-110).

Everything here is in-process and bounded: the production VM has ~270 MB of
free memory, so label sets are kept small on purpose.
"""

from prometheus_client import Counter, Histogram

_STANDARD_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
UNMATCHED_ROUTE = "unmatched"
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
)

capture_tool_duration_seconds = Histogram(
    "lifestack_capture_tool_duration_seconds",
    "Voice-agent tool execution time",
    ["tool", "status"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)


def route_label(request) -> tuple[str, str]:
    """(method, route-template) for a request. The template comes from the matched
    route, never the raw path, so ids and scanner noise cannot mint new series."""
    method = request.method if request.method in _STANDARD_METHODS else "OTHER"
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return method, path if isinstance(path, str) and path else UNMATCHED_ROUTE


def observe_request(request, status_code: int, duration_seconds: float) -> str:
    """Record one finished request; returns the route label (for the log line)."""
    method, route = route_label(request)
    if route not in _EXCLUDED_ROUTES:
        http_request_duration_seconds.labels(
            method=method, route=route, status_class=f"{status_code // 100}xx"
        ).observe(duration_seconds)
    return route


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
