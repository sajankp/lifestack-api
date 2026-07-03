import sys
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import text

from app.application.jobs import (
    bhavcopy_price_feed_job,
    budget_guardrails_job,
    dashboard_insights_job,
    export_cleanup_job,
    fx_rate_ingestion_job,
    import_preview_cleanup_job,
    investment_closing_prices_job,
    push_delivery_job,
    recurring_transactions_job,
    session_cleanup_job,
    todo_reminder_job,
    weekly_summary_job,
)
from app.auth.router import router as auth_router
from app.capture.router import router as capture_router
from app.config import settings
from app.core.database import postgres
from app.core.dependencies import limiter
from app.core.exceptions import (
    APIError,
    api_exception_handler,
    http_exception_handler,
    rate_limit_exception_handler,
    request_validation_exception_handler,
    unhandled_exception_handler,
)
from app.core.health import router as health_router
from app.core.logging import setup_logging
from app.core.middleware import (
    MultipartBodySizeLimitMiddleware,
    SecurityHeadersMiddleware,
    StructlogMiddleware,
)
from app.core.scheduler import (
    register_daily_job,
    register_interval_job,
    scheduler,
    shutdown_scheduler,
    start_scheduler,
)
from app.dashboard.router import router as dashboard_router
from app.exports.router import router as exports_router
from app.finance.router import router as finance_router
from app.imports.router import router as imports_router
from app.investing.router import router as investing_router
from app.notifications.router import router as notifications_router
from app.platform.router import router as platform_router
from app.spending.router import router as spending_router
from app.summaries.router import router as summaries_router
from app.testing.router import router as testing_router
from app.todo.router import router as todo_router

# Initialize logging before creating the app
setup_logging()

logger = structlog.get_logger()


async def _startup_check() -> None:
    """Verify DB connectivity on startup (fail-fast)."""
    try:
        async with postgres.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("startup_readiness_check_passed")
    except Exception as e:
        logger.critical("startup_readiness_check_failed", error=str(e))
        sys.exit(1)

    if settings.METRICS_TOKEN.startswith("dev-") and settings.ENV != "local":
        logger.warning(
            "insecure_metrics_token",
            msg="METRICS_TOKEN is using a default value in a non-local environment.",
        )

    if not settings.SCHEDULER_ENABLED:
        logger.warning(
            "scheduler_disabled",
            msg="SCHEDULER_ENABLED is set to False. Scheduled jobs will not run.",
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await _startup_check()
    if settings.SCHEDULER_ENABLED:
        register_interval_job(
            budget_guardrails_job,
            job_id="budget_guardrails",
            hours=settings.BUDGET_GUARDRAILS_INTERVAL_HOURS,
            idempotent=True,
        )
        register_daily_job(
            recurring_transactions_job,
            job_id="recurring_transactions",
            hour_utc=settings.RECURRING_TXN_GENERATION_HOUR,
        )
        register_daily_job(
            fx_rate_ingestion_job,
            job_id="fx_rate_ingestion",
            hour_utc=2,
        )
        register_daily_job(
            bhavcopy_price_feed_job,
            job_id="bhavcopy_price_feed",
            hour_utc=2,
            minute_utc=0,
        )
        register_daily_job(
            investment_closing_prices_job,
            job_id="investment_closing_prices",
            hour_utc=2,
            minute_utc=30,
        )
        register_daily_job(
            export_cleanup_job,
            job_id="export_cleanup",
            hour_utc=3,
        )
        register_daily_job(
            session_cleanup_job,
            job_id="session_cleanup",
            hour_utc=4,
        )
        register_daily_job(
            import_preview_cleanup_job,
            job_id="import_preview_cleanup",
            hour_utc=5,
        )
        register_daily_job(
            dashboard_insights_job,
            job_id="dashboard_insights",
            hour_utc=6,
        )
        register_interval_job(
            push_delivery_job,
            job_id="push_delivery",
            minutes=settings.PUSH_DELIVERY_INTERVAL_MINUTES,
            idempotent=True,
        )
        register_interval_job(
            todo_reminder_job,
            job_id="todo_reminder",
            minutes=settings.TODO_REMINDER_INTERVAL_MINUTES,
            idempotent=True,
        )
        scheduler.add_job(
            weekly_summary_job,
            "cron",
            day_of_week="mon",
            hour=1,
            minute=30,
            id="weekly_summary",
            replace_existing=True,
            timezone="UTC",
        )
        start_scheduler()
    yield
    if settings.SCHEDULER_ENABLED:
        shutdown_scheduler()


def create_app() -> FastAPI:
    documentation_enabled = settings.ENV != "production"
    _app = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        openapi_url=f"{settings.API_V1_STR}/openapi.json" if documentation_enabled else None,
        docs_url="/docs" if documentation_enabled else None,
        redoc_url="/redoc" if documentation_enabled else None,
        lifespan=lifespan,
        openapi_tags=[
            {
                "name": "capture",
                "description": "Voice-agent capture operations",
            }
        ],
    )

    # CORS
    cors_origins = settings.cors_allowed_origins
    if cors_origins:
        _app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Content-Type",
                "Authorization",
                "X-Requested-With",
                "X-Request-ID",
                "X-CSRF-Token",
                "Origin",
                "Accept",
            ],
        )

    # Core middlewares
    _app.state.limiter = limiter
    _app.add_middleware(SlowAPIMiddleware)
    _app.add_middleware(
        MultipartBodySizeLimitMiddleware,
        max_body_bytes=settings.MAX_MULTIPART_BODY_BYTES,
    )
    _app.add_middleware(SecurityHeadersMiddleware)
    _app.add_middleware(StructlogMiddleware)

    # Exception Handlers
    _app.add_exception_handler(APIError, api_exception_handler)
    _app.add_exception_handler(HTTPException, http_exception_handler)
    _app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    _app.add_exception_handler(Exception, unhandled_exception_handler)
    _app.add_exception_handler(RateLimitExceeded, rate_limit_exception_handler)

    # OpenTelemetry will be initialized after the app starts if configured
    if settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        FastAPIInstrumentor.instrument_app(_app)

    # Authentication and user injection are now handled via FastAPI Depends()
    # Security headers are handled by SecurityHeadersMiddleware

    # Include routers under /v1 prefix
    _app.include_router(auth_router, prefix=f"{settings.API_V1_STR}/auth", tags=["auth"])
    _app.include_router(todo_router, prefix=settings.API_V1_STR)
    _app.include_router(spending_router, prefix=settings.API_V1_STR)
    _app.include_router(investing_router, prefix=settings.API_V1_STR)
    _app.include_router(finance_router, prefix=settings.API_V1_STR)
    _app.include_router(dashboard_router, prefix=settings.API_V1_STR)
    _app.include_router(exports_router, prefix=settings.API_V1_STR)
    _app.include_router(notifications_router, prefix=settings.API_V1_STR)
    _app.include_router(summaries_router, prefix=settings.API_V1_STR)
    _app.include_router(capture_router, prefix=settings.API_V1_STR)
    _app.include_router(imports_router, prefix=settings.API_V1_STR)
    _app.include_router(platform_router, prefix=settings.API_V1_STR)
    if settings.ENABLE_E2E_TEST_HOOKS and settings.ENV in {"local", "test"}:
        _app.include_router(testing_router, prefix=settings.API_V1_STR)

    _app.include_router(health_router)

    return _app


app = create_app()
