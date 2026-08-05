import pytest

from app.config import Settings


def _production_settings(**overrides) -> Settings:
    data = {
        "ENV": "production",
        "SECRET_KEY": "production-secret-key-with-enough-entropy",
        "SESSION_SECRET_KEY": "production-session-secret-with-enough-entropy",
        "GOOGLE_REDIRECT_URI": "https://api.example.com/v1/auth/oauth/google/callback",
        "GITHUB_REDIRECT_URI": "https://api.example.com/v1/auth/oauth/github/callback",
        "METRICS_TOKEN": "production-metrics-token-with-enough-entropy",
        "COOKIE_SECURE": True,
        "COOKIE_DOMAIN": ".sajankp.com",
        "RATE_LIMIT_ENABLED": True,
        "RATE_LIMIT_STORAGE_URI": "redis://redis:6379/1",
    }
    data.update(overrides)
    return Settings(**data)


def test_settings_normalizes_known_env_values():
    settings = Settings(
        ENV="Staging",
        SECRET_KEY="staging-secret-key-changed-in-staging-12345",
        SESSION_SECRET_KEY="staging-session-key-changed-in-staging-12345",
        GOOGLE_REDIRECT_URI="https://api.example.com/v1/auth/oauth/google/callback",
        GITHUB_REDIRECT_URI="https://api.example.com/v1/auth/oauth/github/callback",
        METRICS_TOKEN="staging-metrics-token-changed-in-staging-12345",
        COOKIE_SECURE=True,
        COOKIE_DOMAIN=".sajankp.com",
        RATE_LIMIT_STORAGE_URI="redis://localhost:6379/1",
    )

    assert settings.ENV == "staging"


def test_settings_normalizes_json_string_cors_origins():
    settings = Settings(
        BACKEND_CORS_ORIGINS='["https://www.lifestack.sajankp.com/register","https://lifestack.sajankp.com"]'
    )

    assert settings.cors_allowed_origins == [
        "https://www.lifestack.sajankp.com",
        "https://lifestack.sajankp.com",
    ]


def test_settings_normalizes_wrapped_json_string_csrf_origins():
    settings = Settings(
        CSRF_TRUSTED_ORIGINS='\'["https://www.lifestack.sajankp.com","https://lifestack.sajankp.com/register"]\''
    )

    assert settings.csrf_trusted_origins == [
        "https://www.lifestack.sajankp.com",
        "https://lifestack.sajankp.com",
    ]


def test_settings_normalizes_comma_separated_origin_strings():
    settings = Settings(
        BACKEND_CORS_ORIGINS="https://www.lifestack.sajankp.com, https://lifestack.sajankp.com"
    )

    assert settings.cors_allowed_origins == [
        "https://www.lifestack.sajankp.com",
        "https://lifestack.sajankp.com",
    ]


def test_settings_normalizes_escaped_json_string_origins():
    settings = Settings(
        BACKEND_CORS_ORIGINS='[\\"https://www.lifestack.sajankp.com\\",\\"https://lifestack.sajankp.com\\"]'
    )

    assert settings.cors_allowed_origins == [
        "https://www.lifestack.sajankp.com",
        "https://lifestack.sajankp.com",
    ]


def test_settings_normalizes_bracketed_comma_separated_origins():
    settings = Settings(
        BACKEND_CORS_ORIGINS="[https://www.lifestack.sajankp.com, https://lifestack.sajankp.com]"
    )

    assert settings.cors_allowed_origins == [
        "https://www.lifestack.sajankp.com",
        "https://lifestack.sajankp.com",
    ]


def test_settings_rejects_unknown_env_values():
    with pytest.raises(ValueError, match="ENV must be one of"):
        Settings(ENV="prod")


def test_production_settings_accept_secure_configuration():
    settings = _production_settings()

    assert settings.ENV == "production"


def test_production_settings_reject_default_session_secret():
    with pytest.raises(ValueError, match="SESSION_SECRET_KEY"):
        _production_settings(SESSION_SECRET_KEY="session-secret-key-change-in-production")


def test_production_settings_require_redirect_uri_for_configured_oauth_provider():
    with pytest.raises(ValueError, match="GOOGLE_REDIRECT_URI"):
        _production_settings(
            GOOGLE_CLIENT_ID="google-client-id",
            GOOGLE_CLIENT_SECRET="google-client-secret",
            GOOGLE_REDIRECT_URI="",
        )


def test_production_settings_reject_disabled_rate_limiting():
    with pytest.raises(ValueError, match="RATE_LIMIT_ENABLED"):
        _production_settings(RATE_LIMIT_ENABLED=False)


def test_production_settings_reject_memory_rate_limit_storage():
    with pytest.raises(ValueError, match="RATE_LIMIT_STORAGE_URI"):
        _production_settings(RATE_LIMIT_STORAGE_URI="memory://")


def test_production_settings_reject_insecure_cookies():
    with pytest.raises(ValueError, match="COOKIE_SECURE"):
        _production_settings(COOKIE_SECURE=False)


def test_production_settings_reject_e2e_test_hooks():
    with pytest.raises(ValueError, match="ENABLE_E2E_TEST_HOOKS"):
        _production_settings(ENABLE_E2E_TEST_HOOKS=True)


def test_non_local_settings_reject_e2e_test_hooks():
    with pytest.raises(
        ValueError, match="ENABLE_E2E_TEST_HOOKS must remain disabled in production"
    ):
        Settings(
            ENV="staging",
            ENABLE_E2E_TEST_HOOKS=True,
            SECRET_KEY="staging-secret-key-changed-in-staging-12345",
            SESSION_SECRET_KEY="staging-session-key-changed-in-staging-12345",
            GOOGLE_REDIRECT_URI="https://api.example.com/v1/auth/oauth/google/callback",
            GITHUB_REDIRECT_URI="https://api.example.com/v1/auth/oauth/github/callback",
            METRICS_TOKEN="staging-metrics-token-changed-in-staging-12345",
            COOKIE_SECURE=True,
            COOKIE_DOMAIN=".sajankp.com",
            RATE_LIMIT_STORAGE_URI="redis://localhost:6379/1",
        )
