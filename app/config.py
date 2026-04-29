from urllib.parse import urlparse

from pydantic import AliasChoices, AnyHttpUrl, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    # API Info
    PROJECT_NAME: str = "Lifestack API"
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/v1"

    # CORS
    BACKEND_CORS_ORIGINS: list[AnyHttpUrl] | str = Field(
        default=[],
        validation_alias=AliasChoices("BACKEND_CORS_ORIGINS", "CORS_ORIGINS"),
    )

    CSRF_TRUSTED_ORIGINS: list[AnyHttpUrl] | str = Field(
        default=[],
        validation_alias=AliasChoices("CSRF_TRUSTED_ORIGINS"),
    )

    @computed_field
    @property
    def cors_allowed_origins(self) -> list[str]:
        """Normalize origins so that CORS comparisons ignore paths/trailing slashes."""
        return self._normalize_origins(self.BACKEND_CORS_ORIGINS)

    @computed_field
    @property
    def csrf_trusted_origins(self) -> list[str]:
        """Trusted browser origins for cookie-authenticated mutating requests."""
        if self.CSRF_TRUSTED_ORIGINS:
            return self._normalize_origins(self.CSRF_TRUSTED_ORIGINS)
        return self.cors_allowed_origins

    # Auth
    SECRET_KEY: str = "super-secret-key-change-in-production"
    ACCESS_TOKEN_EXPIRE_SECONDS: int = 60 * 30  # 30 mins
    REFRESH_TOKEN_EXPIRE_SECONDS: int = 60 * 60  # 1 hour

    # Log Level
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/lifestack",
        validation_alias=AliasChoices("DATABASE_URL", "POSTGRES_URL"),
    )

    # Redis (Rate Limiting)
    REDIS_URL: str = "redis://localhost:6379/1"
    RATE_LIMIT_DEFAULT: str = "100/minute"
    RATE_LIMIT_AUTH: str = "10/minute"
    RATE_LIMIT_STORAGE_URI: str = "memory://"  # Set to REDIS_URL in production

    # Cookie Security
    COOKIE_SECURE: bool = False  # Set True in production (HTTPS)

    # Observability
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None

    @computed_field
    @property
    def sync_database_url(self) -> str:
        """Required for Alembic migrations running synchronously."""
        if self.DATABASE_URL.startswith("postgresql+asyncpg://"):
            return self.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        return self.DATABASE_URL

    @staticmethod
    def _normalize_origin(origin: str) -> str:
        """Keep only the scheme and netloc so CORS comparisons match browser origins."""
        if origin == "*":
            return origin

        parsed = urlparse(origin)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("CORS origin must include a scheme and hostname")

        return f"{parsed.scheme}://{parsed.netloc}"

    def _normalize_origins(self, raw_value: list[AnyHttpUrl] | str) -> list[str]:
        if not raw_value:
            return []

        origins = [raw_value] if isinstance(raw_value, str) else list(raw_value)
        sanitized: list[str] = []
        for raw_origin in origins:
            origin_str = str(raw_origin).strip()
            if not origin_str:
                continue

            normalized = self._normalize_origin(origin_str)
            if normalized not in sanitized:
                sanitized.append(normalized)
        return sanitized


settings = Settings()
