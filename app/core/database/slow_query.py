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
from app.observability.request_metrics import db_slow_queries_total

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
    A threshold of 0 (or less) installs nothing."""
    threshold = settings.SLOW_QUERY_THRESHOLD_MS if threshold_ms is None else threshold_ms
    if threshold <= 0:
        return

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
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms >= threshold:
            db_slow_queries_total.inc()
            logger.warning(
                "slow_query",
                duration_ms=round(elapsed_ms, 1),
                statement=_compact(statement),
            )
