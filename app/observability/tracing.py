"""OpenTelemetry tracing + log correlation to PostHog's OTLP endpoint (spec-082).

Inert without ``OTEL_EXPORTER_OTLP_ENDPOINT`` — ``setup_tracing`` is only
called from ``create_app`` behind that check, so dev/test/e2e/CI never
install a real exporter and need no mocking.
"""

import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from app.config import settings
from app.observability.otel_redaction import (  # noqa: F401 — re-exported; tests and callers use these names
    _DENYLIST_KEYS,
    _REDACTED,
    _is_denylisted,
    _parse_otlp_headers,
    redact_span_attributes,
)

_logger = structlog.get_logger(__name__)


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
