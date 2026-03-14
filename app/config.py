from pydantic import AnyHttpUrl, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    # API Info
    PROJECT_NAME: str = "Lifestack API"
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/v1"

    # CORS
    BACKEND_CORS_ORIGINS: list[AnyHttpUrl] | str = []

    # Auth
    SECRET_KEY: str = "super-secret-key-change-in-production"
    ACCESS_TOKEN_EXPIRE_SECONDS: int = 60 * 30  # 30 mins
    REFRESH_TOKEN_EXPIRE_SECONDS: int = 60 * 60  # 1 hour

    # Log Level
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/lifestack"

    # Redis (Rate Limiting)
    REDIS_URL: str = "redis://localhost:6379/1"

    # Observability
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None

    @computed_field
    @property
    def sync_database_url(self) -> str:
        """Required for Alembic migrations running synchronously."""
        if self.DATABASE_URL.startswith("postgresql+asyncpg://"):
            return self.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        return self.DATABASE_URL


settings = Settings()
