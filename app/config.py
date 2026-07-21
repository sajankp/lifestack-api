import json
import secrets
from decimal import Decimal
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
    FRONTEND_URL: str = "http://localhost:5173"

    # Environment
    ENV: str = "local"  # One of: local, staging, production

    # Log Level
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/lifestack",
        validation_alias=AliasChoices("DATABASE_URL", "POSTGRES_URL"),
    )
    DATABASE_POOL_SIZE: int = Field(
        default=0,  # 0 means auto-calculate (CPU * 2)
        validation_alias=AliasChoices("DATABASE_POOL_SIZE", "DB_POOL_SIZE"),
    )
    DATABASE_MAX_OVERFLOW: int = Field(
        default=0,  # 0 means auto-calculate (pool_size)
        validation_alias=AliasChoices("DATABASE_MAX_OVERFLOW", "DB_MAX_OVERFLOW"),
    )

    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_DEFAULT: str = "100/minute"
    RATE_LIMIT_AUTH: str = "10/minute"
    RATE_LIMIT_STORAGE_URI: str = "memory://"  # Set to REDIS_URL in production

    # Cookie Security
    COOKIE_SECURE: bool = False  # Set True in production (HTTPS)
    COOKIE_SAMESITE: str = "lax"
    COOKIE_DOMAIN: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COOKIE_DOMAIN", "CSRF_COOKIE_DOMAIN"),
    )

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
    # Standard OTel env var carrying auth for the OTLP exporter, e.g.
    # "Authorization=Bearer <posthog-project-key>" (spec-082). Unset alongside
    # OTEL_EXPORTER_OTLP_ENDPOINT unset ⇒ no exporter installed.
    OTEL_EXPORTER_OTLP_HEADERS: str | None = None
    METRICS_TOKEN: str = Field(default_factory=lambda: "dev-metrics-" + secrets.token_urlsafe(16))

    # PostHog error tracking (spec-081) — exception capture only, no behavior
    # analytics. Unset ⇒ SDK never initialized (dev/test/e2e/CI default).
    POSTHOG_API_KEY: str | None = None
    POSTHOG_HOST: str = "https://us.i.posthog.com"

    # Resend email notification channel (spec-052 channel_email, spec-081).
    # EMAIL_ENABLED is the explicit master switch so a copied .env can't
    # accidentally start sending; RESEND_API_KEY unset ⇒ deliveries marked
    # skipped regardless of EMAIL_ENABLED.
    RESEND_API_KEY: str | None = None
    EMAIL_FROM_ADDRESS: str | None = None
    EMAIL_ENABLED: bool = False
    EMAIL_DELIVERY_BATCH_CAP: int = 50
    EMAIL_DELIVERY_INTERVAL_MINUTES: int = 1

    # Scheduler
    SCHEDULER_ENABLED: bool = False
    SCHEDULER_ALLOW_NON_IDEMPOTENT_JOBS: bool = False
    BUDGET_GUARDRAILS_INTERVAL_HOURS: int = 6
    BUDGET_WARNING_THRESHOLD: float = 0.9
    BUDGET_CRITICAL_THRESHOLD: float = 1.0

    # Job failure visibility & alerting (spec-088). All fail safe: unset/disabled
    # reverts to today's behavior (no retry, no alert emails), so no
    # production-validator requirement.
    OWNER_ALERT_EMAIL: str | None = None
    JOB_FAILURE_DIGEST_ENABLED: bool = True
    JOB_HEALTH_HEARTBEAT_ENABLED: bool = True
    JOB_RETRY_MAX_ATTEMPTS: int = Field(default=3, ge=1)
    JOB_RETRY_BASE_DELAY_SECONDS: float = Field(default=2.0, ge=0.0)

    # Recurring Transactions (Spec 013)
    RECURRING_TXN_GENERATION_HOUR: int = 0  # UTC hour to run generation job
    RECURRING_TXN_CATCHUP_LIMIT_DAYS: int = 90  # Max days of catch-up generation
    RECURRING_TODO_CATCHUP_LIMIT_DAYS: int = 90  # Max days of catch-up todo generation

    # Web Push notification delivery (spec-052) — push disabled when the VAPID
    # keys are unset; this is the safe default (feature-off, not silently broken).
    VAPID_PUBLIC_KEY: str | None = None
    VAPID_PRIVATE_KEY: str | None = None
    VAPID_SUBJECT: str | None = None  # e.g. "mailto:support@example.com"
    PUSH_DELIVERY_INTERVAL_MINUTES: int = 1
    TODO_REMINDER_INTERVAL_MINUTES: int = 5

    # Morning briefing (spec-067) — daily at ~08:00 IST by default, after
    # Monday's 01:30 UTC weekly_summary cron so a fresh summary lands in
    # that same Monday briefing.
    BRIEFING_JOB_HOUR_UTC: int = 2
    BRIEFING_JOB_MINUTE_UTC: int = 30

    # Health Memory v1 (spec-069)
    HEALTH_DOSE_GRACE_HOURS: int = 4  # owner-confirmed grace window before a dose reads as "missed"
    HEALTH_REMINDER_INTERVAL_MINUTES: int = 5

    # Bulk import storage (Spec 020)
    MAX_MULTIPART_BODY_BYTES: int = 10 * 1024 * 1024
    IMPORT_STORAGE_BACKEND: str = "none"  # none|local|s3
    IMPORT_LOCAL_PATH: str = "/var/lib/lifestack/imports"
    IMPORT_PREVIEW_TTL_HOURS: int = 24
    RUN_BACKGROUND_TASKS_SYNCHRONOUSLY: bool = False

    # Session limits
    MAX_ACTIVE_SESSIONS_PER_USER: int = 5
    ENABLE_DEMO_RESET: bool = False
    ENABLE_E2E_TEST_HOOKS: bool = False

    # Security reference data (Spec 083): bundled data works fully offline by
    # default; the Yahoo quote/identity API fallback is opt-in.
    REFERENCE_DATA_API_ENABLED: bool = False
    REFERENCE_DATA_CACHE_STALENESS_DAYS: int = 30

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
    GEMINI_LIVE_URL: str = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    # Pin to an explicit versioned model — do NOT use a -latest alias here.
    # Google can silently rotate or deprecate -latest aliases. If a specific versioned
    # model becomes unavailable, update this default or set GEMINI_MODEL in .env.
    GEMINI_MODEL: str = "models/gemini-2.5-flash-preview-native-audio-18-12"
    # Thinking-token budget for the live model's tool-call reasoning (spec-059).
    # A modest budget improves tool-argument quality; set 0 to disable thinking
    # entirely if the configured model rejects a non-zero value.
    GEMINI_THINKING_BUDGET: int = 256
    CAPTURE_MAX_WS_FRAME_BYTES: int = 256 * 1024
    CAPTURE_MAX_SESSION_BYTES: int = 15 * 1024 * 1024
    CAPTURE_MAX_SESSION_SECONDS: int = 5 * 60
    CAPTURE_MAX_TEXT_CHARS: int = 4000
    # spec-079 Stage A: append-only JSONL log of each voice tool-call turn
    # (tool name + args + status — no raw utterance text yet, see spec-079),
    # for building the real-usage eval slice and debugging routing. Feature-off
    # (no writes) unless set; production points this at a bind-mounted host
    # path so it survives container recreation (docker-compose.yml/.prod.yml).
    CAPTURE_TURN_LOG_PATH: str | None = None
    # spec-079 Stage B: enable Gemini output-audio transcription so the model's
    # spoken reply is captured as text — for live captions and the assistant
    # side of the capture log. Metered free (spec-079, 2026-07-13). Defaults to
    # current behavior (off); enable in prod after confirming the deployed model
    # accepts `outputAudioTranscription`.
    CAPTURE_ENABLE_OUTPUT_TRANSCRIPTION: bool = False
    # spec-079 Q4 (input direction, resolved 2026-07-17): enable Gemini
    # input-audio transcription so the user's own utterance is captured as text
    # into the capture log (`kind='user_transcript'`) — the source for the
    # real-usage eval slice. Metered free (delta within baseline jitter;
    # 3.1 Flash Live emits inputTranscription even when not requested).
    # Defaults to current behavior (off).
    CAPTURE_ENABLE_INPUT_TRANSCRIPTION: bool = False
    # spec-079 Stage B: WebSocket transport resilience. Both default to current
    # behavior (off) per the spec's "new limit defaults to current behavior" rule.
    # Session resumption opts the Gemini Live session in to periodic resumption
    # handles; the handle is round-tripped through the client so an interrupted
    # session can reconnect with its conversation context intact (see
    # `app/capture/agent.py`). Context-window compression enables a sliding window
    # so long sessions are not terminated at the model's context limit. Enable in
    # prod only after confirming the deployed model accepts these setup fields.
    CAPTURE_ENABLE_SESSION_RESUMPTION: bool = False
    CAPTURE_ENABLE_CONTEXT_COMPRESSION: bool = False
    EXCHANGERATE_API_KEY: str | None = None
    LOOKTHROUGH_MIN_DISPLAY_WEIGHT_PCT: Decimal = Decimal("0.5")
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

        origins = self._coerce_origin_values(raw_value)
        sanitized: list[str] = []
        for raw_origin in origins:
            origin_str = str(raw_origin).strip()
            if not origin_str:
                continue

            normalized = self._normalize_origin(origin_str)
            if normalized not in sanitized:
                sanitized.append(normalized)
        return sanitized

    @staticmethod
    def _coerce_origin_values(raw_value: list[AnyHttpUrl] | str) -> list[str | AnyHttpUrl]:
        if not isinstance(raw_value, str):
            return list(raw_value)

        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1].strip()

        if value.startswith("["):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                try:
                    parsed = json.loads(value.replace(r"\"", '"'))
                except json.JSONDecodeError:
                    parsed = None
            if isinstance(parsed, list):
                return [str(origin).strip() for origin in parsed if str(origin).strip()]

            if value.endswith("]"):
                value = value[1:-1].strip()

        if "," in value:
            return [
                Settings._strip_origin_wrapping(origin)
                for origin in value.split(",")
                if Settings._strip_origin_wrapping(origin)
            ]

        return [Settings._strip_origin_wrapping(value)]

    @staticmethod
    def _strip_origin_wrapping(value: str) -> str:
        origin = value.strip().strip("'\"").strip()
        if origin.startswith(r"\"") and origin.endswith(r"\""):
            origin = origin[2:-2].strip()
        return origin.strip("'\"").strip()

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
        if self.ENV in ("production", "staging"):
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
            if not self.COOKIE_DOMAIN:
                raise ValueError(
                    "COOKIE_DOMAIN must be set in production (e.g. .sajankp.com) to allow cross-subdomain CSRF cookies."
                )
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
