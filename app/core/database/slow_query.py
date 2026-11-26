"""Slow-query logging (spec-110).

Logs any SQL statement slower than a threshold, with the statement text
collapsed to one line and truncated, and counts it for Prometheus. Bind
parameters are deliberately never logged: they carry amounts, names and tokens.
"""

import time

import structlog
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.config import settings
from app.observability.db_profile import record_query
from app.observability.request_metrics import NO_ROUTE, db_slow_queries_total, route_for_scope

logger = structlog.get_logger(__name__)

_MAX_STATEMENT_CHARS = 300
_START_ATTR = "_lifestack_query_started"


def _compact(statement: str) -> str:
    one_line = " ".join(statement.split())
    if len(one_line) > _MAX_STATEMENT_CHARS:
        return one_line[:_MAX_STATEMENT_CHARS] + "…"
    return one_line


def install_slow_query_logging(engine: Engine, threshold_ms: float | None = None) -> None:
    """Attach the timing listeners to a *sync* engine (use `async_engine.sync_engine`).

    Every statement is added to the active request's DB profile (spec-112). A threshold of 0
    (or less) only turns the slow-query log and counter off; the profile is still recorded."""
    threshold = settings.SLOW_QUERY_THRESHOLD_MS if threshold_ms is None else threshold_ms

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        # Stored on the per-execution context, so a failed statement leaves no
        # state behind (after_cursor_execute does not fire on error).
        setattr(context, _START_ATTR, time.perf_counter())

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        started = getattr(context, _START_ATTR, None)
        if started is None:
            return
        elapsed_s = time.perf_counter() - started
        profile = record_query(elapsed_s)
        elapsed_ms = elapsed_s * 1000
        if threshold > 0 and elapsed_ms >= threshold:
            route = route_for_scope(profile.scope) if profile else NO_ROUTE
            db_slow_queries_total.labels(route=route).inc()
            logger.warning(
                "slow_query",
                duration_ms=round(elapsed_ms, 1),
                route=route,
                statement=_compact(statement),
            )
