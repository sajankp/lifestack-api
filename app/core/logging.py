import logging
import sys

import structlog

from app.config import settings


class HealthAccessFilter(logging.Filter):
    """Drop successful health-probe entries from Uvicorn's access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            _client, _method, path, _http_version, status_code = record.args
        except (TypeError, ValueError):
            return True

        normalized_path = str(path).partition("?")[0].rstrip("/") or "/"
        return normalized_path != "/health" or int(status_code) >= 400


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
