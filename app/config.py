import secrets
from urllib.parse import urlparse

import structlog
from pydantic import (
    AliasChoices,
    AnyHttpUrl,
    Field,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

_logger = structlog.get_logger(__name__)


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
    REFRESH_TOKEN_EXPIRE_SECONDS: int = 60 * 60 * 24 * 7  # 7 days

    # Environment
    ENV: str = "local"  # One of: local, staging, production

    # Log Level
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/lifestack",
        validation_alias=AliasChoices("DATABASE_URL", "POSTGRES_URL"),
    )

    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_DEFAULT: str = "100/minute"
    RATE_LIMIT_AUTH: str = "10/minute"
    RATE_LIMIT_STORAGE_URI: str = "memory://"  # Set to REDIS_URL in production

    # Cookie Security
    COOKIE_SECURE: bool = False  # Set True in production (HTTPS)
    COOKIE_SAMESITE: str = "lax"
    COOKIE_DOMAIN: str | None = None

    # Trusted Proxies (Spec 025)
    TRUSTED_PROXIES: list[str] = ["127.0.0.1", "::1", "testclient"]

    # CSP Configuration
    CSP_IMG_SRC: str = (
        ""  # Extra img-src sources (e.g. CDN URLs); 'self' and data: are always included
    )
    CSP_STYLE_SRC: str = "'unsafe-inline'"
    CSP_SCRIPT_SRC: str = ""
    CSP_FONT_SRC: str = ""
    CSP_CONNECT_SRC: str = ""

    # Observability
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None
    METRICS_TOKEN: str = Field(default_factory=lambda: "dev-metrics-" + secrets.token_urlsafe(16))

    # Scheduler
    SCHEDULER_ENABLED: bool = False
    SCHEDULER_ALLOW_NON_IDEMPOTENT_JOBS: bool = False
    BUDGET_GUARDRAILS_INTERVAL_HOURS: int = 6
    BUDGET_WARNING_THRESHOLD: float = 0.9
    BUDGET_CRITICAL_THRESHOLD: float = 1.0

    # Recurring Transactions (Spec 013)
    RECURRING_TXN_GENERATION_HOUR: int = 0  # UTC hour to run generation job
    RECURRING_TXN_CATCHUP_LIMIT_DAYS: int = 90  # Max days of catch-up generation
    RECURRING_TODO_CATCHUP_LIMIT_DAYS: int = 90  # Max days of catch-up todo generation

    # Bulk import storage (Spec 020)
    MAX_MULTIPART_BODY_BYTES: int = 10 * 1024 * 1024
    IMPORT_STORAGE_BACKEND: str = "none"  # none|local|s3
    IMPORT_LOCAL_PATH: str = "/var/lib/lifestack/imports"
    IMPORT_PREVIEW_TTL_HOURS: int = 24

    # Session limits
    MAX_ACTIVE_SESSIONS_PER_USER: int = 5
    ENABLE_DEMO_RESET: bool = False
    ENABLE_E2E_TEST_HOOKS: bool = False

    # Export storage hardening (Spec 006)
    EXPORT_STORAGE_BACKEND: str = "db"  # db|local|s3
    EXPORT_LOCAL_PATH: str = "/var/lib/lifestack/exports"
    EXPORT_TTL_DAYS: int = 365
    EXPORT_CLEANUP_ENABLED: bool = True
    EXPORT_CLEANUP_DELETE_FILES: bool = True
    EXPORT_CLEANUP_DELETE_RECORDS: bool = False

    EXPORT_S3_ENDPOINT: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "EXPORT_S3_ENDPOINT", "IMPORT_S3_ENDPOINT", "CLOUDFLARE_R2_ENDPOINT"
        ),
    )
    EXPORT_S3_BUCKET: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "EXPORT_S3_BUCKET", "IMPORT_S3_BUCKET", "CLOUDFLARE_R2_BUCKET"
        ),
    )
    EXPORT_S3_REGION: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "EXPORT_S3_REGION", "IMPORT_S3_REGION", "CLOUDFLARE_R2_REGION"
        ),
    )
    EXPORT_S3_ACCESS_KEY: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "EXPORT_S3_ACCESS_KEY", "IMPORT_S3_ACCESS_KEY", "CLOUDFLARE_R2_ACCESS_KEY"
        ),
    )
    EXPORT_S3_SECRET_KEY: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "EXPORT_S3_SECRET_KEY", "IMPORT_S3_SECRET_KEY", "CLOUDFLARE_R2_SECRET_KEY"
        ),
    )
    EXPORT_S3_FORCE_PATH_STYLE: bool = False

    # AI Voice Agent (Spec 021)
    GEMINI_API_KEY: str | None = None
    CAPTURE_MAX_WS_FRAME_BYTES: int = 256 * 1024
    CAPTURE_MAX_SESSION_BYTES: int = 15 * 1024 * 1024
    CAPTURE_MAX_SESSION_SECONDS: int = 5 * 60
    CAPTURE_MAX_TEXT_CHARS: int = 4000
    EXCHANGERATE_API_KEY: str | None = None
    IMPORT_S3_ENDPOINT: str | None = Field(
        default=None,
        validation_alias=AliasChoices("IMPORT_S3_ENDPOINT", "CLOUDFLARE_R2_ENDPOINT"),
    )
    IMPORT_S3_BUCKET: str | None = Field(
        default=None,
        validation_alias=AliasChoices("IMPORT_S3_BUCKET", "CLOUDFLARE_R2_BUCKET"),
    )
    IMPORT_S3_REGION: str | None = Field(
        default=None,
        validation_alias=AliasChoices("IMPORT_S3_REGION", "CLOUDFLARE_R2_REGION"),
    )
    IMPORT_S3_ACCESS_KEY: str | None = Field(
        default=None,
        validation_alias=AliasChoices("IMPORT_S3_ACCESS_KEY", "CLOUDFLARE_R2_ACCESS_KEY"),
    )
    IMPORT_S3_SECRET_KEY: str | None = Field(
        default=None,
        validation_alias=AliasChoices("IMPORT_S3_SECRET_KEY", "CLOUDFLARE_R2_SECRET_KEY"),
    )
    IMPORT_S3_FORCE_PATH_STYLE: bool = False

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

    @model_validator(mode="before")
    @classmethod
    def _parse_trusted_proxies(cls, data: dict) -> dict:
        trusted_proxies = data.get("TRUSTED_PROXIES")
        if isinstance(trusted_proxies, str):
            data["TRUSTED_PROXIES"] = [
                ip.strip() for ip in trusted_proxies.split(",") if ip.strip()
            ]
        return data

    @field_validator("ENV")
    @classmethod
    def _normalize_env(cls, value: str) -> str:
        normalized_env = value.strip().lower()
        if normalized_env not in {"local", "staging", "production", "test"}:
            raise ValueError("ENV must be one of: local, staging, production, test.")
        return normalized_env

    @model_validator(mode="after")
    def _check_production_defaults(self) -> "Settings":
        """Fail fast when insecure defaults are used in production."""
        if self.ENV == "production":
            if self.SECRET_KEY == "super-secret-key-change-in-production":
                raise ValueError("SECRET_KEY must be changed from its default value in production.")
            if self.METRICS_TOKEN.startswith("dev-"):
                raise ValueError(
                    "METRICS_TOKEN must be changed from its default value in production."
                )
            if not self.COOKIE_SECURE:
                raise ValueError("COOKIE_SECURE must be enabled (True) in production.")
            if not self.RATE_LIMIT_ENABLED:
                raise ValueError("RATE_LIMIT_ENABLED must remain enabled in production.")
            if self.RATE_LIMIT_STORAGE_URI == "memory://":
                raise ValueError(
                    "RATE_LIMIT_STORAGE_URI must be configured (non-memory) in production."
                )
            if self.ENABLE_E2E_TEST_HOOKS:
                raise ValueError("ENABLE_E2E_TEST_HOOKS must remain disabled in production.")
        else:
            if self.ENABLE_E2E_TEST_HOOKS and self.ENV not in {"local", "test"}:
                raise ValueError("ENABLE_E2E_TEST_HOOKS is only allowed in local/test.")

            # Fallback warning for non-local database when ENV is not production
            parsed_db = urlparse(self.DATABASE_URL)
            is_local_db = parsed_db.hostname in ("localhost", "127.0.0.1", "postgres")
            if not is_local_db:
                if self.SECRET_KEY == "super-secret-key-change-in-production":
                    raise ValueError(
                        "SECRET_KEY must be changed from its default value "
                        "when DATABASE_URL points to a remote host."
                    )
                if self.METRICS_TOKEN.startswith("dev-"):
                    raise ValueError(
                        "METRICS_TOKEN must be changed from its default value "
                        "when DATABASE_URL points to a remote host."
                    )
        return self


settings = Settings()
