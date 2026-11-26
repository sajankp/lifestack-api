import logging
import sys

import structlog

from app.config import settings
from app.observability.log_export import otel_log_processor


class HealthAccessFilter(logging.Filter):
    """Drop successful health-probe entries from Uvicorn's access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            _client, _method, path, _http_version, status_code = record.args
            status_code_int = int(status_code)
        except (TypeError, ValueError):
            return True

        normalized_path = str(path).partition("?")[0].rstrip("/") or "/"
        return normalized_path != "/health" or status_code_int >= 400


def warn_if_slow_query_log_suppressed() -> None:
    """Make a silent misconfiguration loud (spec-112).

    `slow_query` is logged at WARNING. With LOG_LEVEL above that, structlog drops the line while
    the Prometheus counter keeps counting, so production showed 1,141 slow queries and no
    statements. Emit at a level that always passes the filter."""
    level = getattr(logging, str(settings.LOG_LEVEL).upper(), logging.INFO)
    if settings.SLOW_QUERY_THRESHOLD_MS <= 0 or level <= logging.WARNING:
        return
    logger = structlog.get_logger()
    emit = logger.critical if level >= logging.CRITICAL else logger.error
    emit(
        "slow_query_log_suppressed_by_log_level",
        log_level_setting=settings.LOG_LEVEL,
        slow_query_threshold_ms=settings.SLOW_QUERY_THRESHOLD_MS,
        hint="set LOG_LEVEL=WARNING (or lower) to see slow_query statements",
    )


def setup_logging():
    """Configure structlog for consistent structured logging."""

    # Standard library logging config
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=settings.LOG_LEVEL,
    )
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, HealthAccessFilter) for item in access_logger.filters):
        access_logger.addFilter(HealthAccessFilter())

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="iso"),
        # spec-082: mirrors each event to OTel logs (no-op until
        # setup_log_export() runs); does not alter the event dict, so stdout
        # rendering below is unaffected.
        otel_log_processor,
    ]

    if settings.LOG_LEVEL == "DEBUG":
        # Pretty printing for development
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        # JSON for production
        processors.append(structlog.processors.dict_tracebacks)
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, settings.LOG_LEVEL)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    warn_if_slow_query_log_suppressed()
