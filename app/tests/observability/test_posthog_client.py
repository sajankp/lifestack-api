from unittest.mock import MagicMock, patch

from app.config import settings
from app.observability import posthog_client


def test_capture_exception_inert_without_api_key():
    posthog_client.reset_for_tests()
    original = settings.POSTHOG_API_KEY
    settings.POSTHOG_API_KEY = None
    try:
        posthog_client.init_posthog()
        # Must never raise, and must not touch the (unset) SDK.
        posthog_client.capture_exception(ValueError("boom"), route="/v1/x")
    finally:
        settings.POSTHOG_API_KEY = original
        posthog_client.reset_for_tests()


def test_capture_exception_forwards_to_sdk_when_configured():
    posthog_client.reset_for_tests()
    original = settings.POSTHOG_API_KEY
    settings.POSTHOG_API_KEY = "test-key"
    try:
        with patch("app.observability.posthog_client._client") as mock_client:
            posthog_client._initialized = True
            exc = ValueError("boom")
            posthog_client.capture_exception(exc, route="/v1/x")
            mock_client.capture_exception.assert_called_once()
            _, kwargs = mock_client.capture_exception.call_args
            assert kwargs["properties"] == {"route": "/v1/x"}
    finally:
        settings.POSTHOG_API_KEY = original
        posthog_client.reset_for_tests()


def test_capture_exception_never_raises_when_sdk_call_fails():
    posthog_client.reset_for_tests()
    mock_client = MagicMock()
    mock_client.capture_exception.side_effect = RuntimeError("sdk broke")
    posthog_client._client = mock_client
    posthog_client._initialized = True
    try:
        posthog_client.capture_exception(ValueError("boom"))
    finally:
        posthog_client.reset_for_tests()


def test_init_posthog_is_idempotent():
    posthog_client.reset_for_tests()
    original = settings.POSTHOG_API_KEY
    settings.POSTHOG_API_KEY = "test-key"
    try:
        posthog_client.init_posthog()
        first_client = posthog_client._client
        posthog_client.init_posthog()
        assert posthog_client._client is first_client
    finally:
        settings.POSTHOG_API_KEY = original
        posthog_client.reset_for_tests()
