"""PostHog error-tracking client (spec-081) — exception capture only.

Backend scope is exception capture, not behavior analytics (that lives in the
frontend, see `src/main.tsx`). Inert unless ``POSTHOG_API_KEY`` is set: no
network calls at import time, and ``capture_exception`` never raises into the
caller's request/job path — a broken PostHog SDK must never turn into a
second outage on top of the one it's trying to report.
"""

import posthog as posthog_sdk
import structlog

from app.config import settings

_logger = structlog.get_logger(__name__)

_client = None
_initialized = False

# No per-user analytics happens server-side (spec-081: "no server-side
# behavior analytics") — a fixed distinct_id is enough to group exceptions
# under one PostHog project.
_SERVER_DISTINCT_ID = "lifestack-api"


def init_posthog() -> None:
    """Initialize the PostHog SDK once, at app startup, if configured."""
    global _client, _initialized
    if _initialized:
        return
    _initialized = True
    if not settings.POSTHOG_API_KEY:
        return

    posthog_sdk.api_key = settings.POSTHOG_API_KEY
    posthog_sdk.host = settings.POSTHOG_HOST
    _client = posthog_sdk


def capture_exception(exc: BaseException, **properties: object) -> None:
    """Report an exception to PostHog. No-op without ``POSTHOG_API_KEY``.

    ``properties`` must never carry request bodies, financial values, or PII
    (spec-081 privacy rule) — callers pass route/job names and status codes
    only.
    """
    if _client is None:
        return
    try:
        _client.capture_exception(exc, distinct_id=_SERVER_DISTINCT_ID, properties=properties)
    except Exception:
        _logger.warning("posthog_capture_exception_failed", exc_info=True)


def reset_for_tests() -> None:
    """Test-only hook to re-run ``init_posthog`` after monkeypatching settings."""
    global _client, _initialized
    _client = None
    _initialized = False
