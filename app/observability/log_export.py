"""Ship structlog output as OTel logs (spec-082), correlated to the active
trace/span via OTel's ambient context — local stdout logging (configured
separately in ``app.core.logging``) is untouched by this module.

A dedicated stdlib logger/handler pair carries events into the OTel Logs SDK;
nothing is attached to the root logger, so this has zero effect on the
existing print/stdout pipeline.
"""

import logging

from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource

from app.config import settings
from app.observability.tracing import _parse_otlp_headers, redact_span_attributes

_OTEL_BRIDGE_LOGGER_NAME = "lifestack.otel_bridge"
_bridge_logger: logging.Logger | None = None


def setup_log_export() -> None:
    global _bridge_logger
    if _bridge_logger is not None:
        return

    resource = Resource.create({
        "service.name": "lifestack-api",
        "deployment.environment": settings.ENV,
    })
    provider = LoggerProvider(resource=resource)
    exporter = OTLPLogExporter(
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        headers=_parse_otlp_headers(settings.OTEL_EXPORTER_OTLP_HEADERS),
    )
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(provider)

    bridge_logger = logging.getLogger(_OTEL_BRIDGE_LOGGER_NAME)
    bridge_logger.setLevel(logging.DEBUG)
    bridge_logger.propagate = False  # never reaches the root logger's stdout handler
    bridge_logger.addHandler(LoggingHandler(logger_provider=provider))
    _bridge_logger = bridge_logger


# stdlib LogRecord attribute names — passing one of these via `extra` raises
# KeyError, and several (name, module, message) collide with common
# structlog event-dict keys (e.g. spec-052's user/notification "module").
_RESERVED_LOG_RECORD_ATTRS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "asctime",
}

_LEVELNO = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def otel_log_processor(logger, method_name, event_dict):
    """A structlog processor: forwards each event to the OTel bridge logger
    (no-op until ``setup_log_export`` has run) and passes the event through
    unchanged for the normal stdout renderer."""
    if _bridge_logger is not None:
        try:
            attributes = redact_span_attributes({
                k: v
                for k, v in event_dict.items()
                if k != "event" and k not in _RESERVED_LOG_RECORD_ATTRS
            })
            level = _LEVELNO.get(method_name, logging.INFO)
            _bridge_logger.log(level, event_dict.get("event", ""), extra=attributes)
        except Exception:
            pass
    return event_dict


def reset_for_tests() -> None:
    global _bridge_logger
    _bridge_logger = None
