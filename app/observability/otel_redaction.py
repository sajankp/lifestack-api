"""Pure helpers shared by OTel tracing and log export (spec-082).

Deliberately free of any ``opentelemetry`` import: ``app.core.logging`` and
``app.observability.log_export`` need these on every start-up, while the OTel SDK itself
(~7 MB resident) is only loaded when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set
(see app/tests/test_import_footprint.py).
"""

import re

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
