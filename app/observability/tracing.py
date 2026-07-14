"""OpenTelemetry tracing + log correlation to PostHog's OTLP endpoint (spec-082).

Inert without ``OTEL_EXPORTER_OTLP_ENDPOINT`` — ``setup_tracing`` is only
called from ``create_app`` behind that check, so dev/test/e2e/CI never
install a real exporter and need no mocking.
"""

import re

import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from app.config import settings

_logger = structlog.get_logger(__name__)

# Attribute keys that must never leave the box, plus a query-string /
# request-body catch-all pattern (spec-082 privacy section — the core
# design problem of this spec).
_DENYLIST_KEYS = {"amount", "balance", "email", "token", "name", "note", "body"}
_REDACTED = "[redacted]"


def _parse_otlp_headers(raw: str | None) -> dict[str, str]:
    """Parse the standard OTel comma-separated ``key=value`` header format."""
    headers: dict[str, str] = {}
    if not raw:
        return headers
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        headers[key.strip()] = value.strip()
    return headers


def _is_denylisted(key: str) -> bool:
    lowered = key.lower()
    # Allow-list common non-PII metadata suffixes that would otherwise match
    # the "name" denylist term (e.g. job_name, logger_name, method_name).
    safe_suffixes = (
        "job_name",
        "logger_name",
        "method_name",
        "class_name",
        "db_name",
        "funcname",
        "filename",
        "pathname",
    )
    if any(lowered.endswith(s) or lowered == s for s in safe_suffixes):
        return False
    return any(term in lowered for term in _DENYLIST_KEYS)


def redact_span_attributes(attributes: dict) -> dict:
    """Strip denylisted attribute values and query strings from a span's
    attributes. Used both by the exporting processor and directly in tests."""
    redacted = {}
    for key, value in attributes.items():
        if _is_denylisted(key):
            redacted[key] = _REDACTED
        elif key in ("http.url", "http.target", "url.full") and isinstance(value, str):
            redacted[key] = re.sub(r"\?.*$", "", value)
        else:
            redacted[key] = value
    return redacted


class RedactingSpanExporter(SpanExporter):
    """Wraps a real exporter, redacting span attributes before export —
    never trust the instrumentation defaults not to leak query strings,
    bound SQL parameters, or PII-shaped attribute values."""

    def __init__(self, wrapped: SpanExporter):
        self._wrapped = wrapped

    def export(self, spans) -> SpanExportResult:
        redacted_spans = [_redact_span(span) for span in spans]
        return self._wrapped.export(redacted_spans)

    def shutdown(self) -> None:
        self._wrapped.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._wrapped.force_flush(timeout_millis)


def _redact_span(span: ReadableSpan) -> ReadableSpan:
    if not span.attributes:
        return span
    redacted_attrs = redact_span_attributes(dict(span.attributes))
    span._attributes = redacted_attrs  # noqa: SLF001 — ReadableSpan has no public mutator
    return span


_tracer_provider: TracerProvider | None = None


def setup_tracing() -> TracerProvider:
    """Configure a real TracerProvider + batch OTLP exporter, replacing the
    default no-op provider. Idempotent — safe to call once at startup."""
    global _tracer_provider
    if _tracer_provider is not None:
        return _tracer_provider

    resource = Resource.create({
        "service.name": "lifestack-api",
        "deployment.environment": settings.ENV,
    })
    provider = TracerProvider(resource=resource)
    exporter = RedactingSpanExporter(
        OTLPSpanExporter(
            endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
            headers=_parse_otlp_headers(settings.OTEL_EXPORTER_OTLP_HEADERS),
        )
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer_provider = provider
    return provider


def reset_for_tests() -> None:
    global _tracer_provider
    _tracer_provider = None
