import pytest

from app.config import Settings


def _production_settings(**overrides) -> Settings:
    data = {
        "ENV": "production",
        "SECRET_KEY": "production-secret-key-with-enough-entropy",
        "METRICS_TOKEN": "production-metrics-token-with-enough-entropy",
        "COOKIE_SECURE": True,
        "RATE_LIMIT_ENABLED": True,
        "RATE_LIMIT_STORAGE_URI": "redis://redis:6379/1",
    }
    data.update(overrides)
    return Settings(**data)


def test_settings_normalizes_known_env_values():
    settings = Settings(ENV="Staging")

    assert settings.ENV == "staging"


def test_settings_rejects_unknown_env_values():
    with pytest.raises(ValueError, match="ENV must be one of"):
        Settings(ENV="prod")


def test_production_settings_accept_secure_configuration():
    settings = _production_settings()

    assert settings.ENV == "production"


def test_production_settings_reject_disabled_rate_limiting():
    with pytest.raises(ValueError, match="RATE_LIMIT_ENABLED"):
        _production_settings(RATE_LIMIT_ENABLED=False)


def test_production_settings_reject_memory_rate_limit_storage():
    with pytest.raises(ValueError, match="RATE_LIMIT_STORAGE_URI"):
        _production_settings(RATE_LIMIT_STORAGE_URI="memory://")


def test_production_settings_reject_insecure_cookies():
    with pytest.raises(ValueError, match="COOKIE_SECURE"):
        _production_settings(COOKIE_SECURE=False)
