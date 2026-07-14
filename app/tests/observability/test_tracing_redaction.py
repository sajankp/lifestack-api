from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.config import settings
from app.observability import tracing


def test_redact_span_attributes_masks_denylisted_keys():
    attrs = {
        "http.method": "POST",
        "user.email": "person@example.com",
        "transaction.amount": "1234.56",
        "account.balance": "5000",
        "auth.token": "secret-token",
        "user.name": "Alice",
        "todo.note": "buy milk",
        "request.body": '{"foo": "bar"}',
        "workspace_id": 42,
    }
    redacted = tracing.redact_span_attributes(attrs)

    assert redacted["http.method"] == "POST"
    assert redacted["workspace_id"] == 42
    for key in (
        "user.email",
        "transaction.amount",
        "account.balance",
        "auth.token",
        "user.name",
        "todo.note",
        "request.body",
    ):
        assert redacted[key] == tracing._REDACTED, key


def test_redact_span_attributes_strips_query_strings():
    attrs = {"http.url": "https://api.example.com/v1/todo?token=abc&foo=bar"}
    redacted = tracing.redact_span_attributes(attrs)
    assert redacted["http.url"] == "https://api.example.com/v1/todo"


def test_parse_otlp_headers():
    assert tracing._parse_otlp_headers(None) == {}
    assert tracing._parse_otlp_headers("") == {}
    assert tracing._parse_otlp_headers("Authorization=Bearer abc123") == {
        "Authorization": "Bearer abc123"
    }
    assert tracing._parse_otlp_headers("a=1,b=2") == {"a": "1", "b": "2"}


def test_redacting_exporter_strips_denylisted_values_before_export():
    """A request through an instrumented app must produce exported spans
    containing zero denylisted values (spec-082 testing requirement)."""
    in_memory = InMemorySpanExporter()
    redacting = tracing.RedactingSpanExporter(in_memory)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(redacting))
    tracer = provider.get_tracer(__name__)

    with tracer.start_as_current_span("test-span") as span:
        span.set_attribute("user.email", "person@example.com")
        span.set_attribute("http.method", "GET")

    (exported,) = in_memory.get_finished_spans()
    assert exported.attributes["user.email"] == tracing._REDACTED
    assert exported.attributes["http.method"] == "GET"


def test_setup_tracing_installs_real_provider_when_configured():
    tracing.reset_for_tests()
    original_endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT
    original_headers = settings.OTEL_EXPORTER_OTLP_HEADERS
    settings.OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4318/v1/traces"
    settings.OTEL_EXPORTER_OTLP_HEADERS = "Authorization=Bearer test"
    try:
        provider = tracing.setup_tracing()
        assert isinstance(provider, tracing.TracerProvider)
        # Idempotent — second call returns the same provider, not a new one.
        assert tracing.setup_tracing() is provider
    finally:
        settings.OTEL_EXPORTER_OTLP_ENDPOINT = original_endpoint
        settings.OTEL_EXPORTER_OTLP_HEADERS = original_headers
        tracing.reset_for_tests()
