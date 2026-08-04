import sys
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import text

from app.application.jobs import (
    bhavcopy_price_feed_job,
    budget_guardrails_job,
    dashboard_insights_job,
    email_delivery_job,
    export_cleanup_job,
    fx_rate_ingestion_job,
    import_preview_cleanup_job,
    investment_closing_prices_job,
    job_failure_digest_job,
    job_health_heartbeat_job,
    kpi_guardrails_job,
    medication_reminder_job,
    morning_briefing_job,
    net_worth_snapshot_job,
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
from app.core.etag import ETagMiddleware
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
from app.health.router import router as health_module_router
from app.imports.router import router as imports_router
from app.investing.router import router as investing_router
from app.mcp.server import create_mcp_asgi_app, create_mcp_server
from app.notifications.router import router as notifications_router
from app.observability.log_export import setup_log_export
from app.observability.posthog_client import init_posthog
from app.observability.scheduler_metrics import set_jobs_registered, set_scheduler_running
from app.observability.tracing import setup_tracing
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
async def _combined_lifespan(_app: FastAPI):
    """Combined lifespan for FastAPI + MCP server."""
    # Startup
    await _startup_check()

    # Initialize scheduler if enabled
    if settings.SCHEDULER_ENABLED:
        _register_scheduled_jobs()
        start_scheduler()

    yield

    # Shutdown
    if settings.SCHEDULER_ENABLED:
        shutdown_scheduler()
        set_scheduler_running(False)


# Create MCP app outside of lifespan (needs to exist before FastAPI creation)
_mcp_app = None
if settings.MCP_ENABLED:
    _mcp = create_mcp_server()
    _mcp_app = create_mcp_asgi_app(_mcp)

# Create the final lifespan that combines FastAPI + MCP
if _mcp_app:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with _combined_lifespan(_app), _mcp_app.lifespan(_app):
            yield
else:
    lifespan = _combined_lifespan


def _register_scheduled_jobs():
    """Register all scheduled jobs."""
    register_interval_job(
        budget_guardrails_job,
        job_id="budget_guardrails",
        hours=settings.BUDGET_GUARDRAILS_INTERVAL_HOURS,
        idempotent=True,
    )
    register_interval_job(
        kpi_guardrails_job,
        job_id="kpi_guardrails",
        hours=settings.BUDGET_GUARDRAILS_INTERVAL_HOURS,
        idempotent=True,
    )
    # spec-089: deterministic IST-morning window (03:00-05:30 IST =
    # 21:30-00:00 UTC), fixed (no jitter), ordered by real data
    # dependencies -- not the calendar order above. Jitter was removed
    # here because it broke a real ordering guarantee: bhavcopy_price_feed
    # (the preferred official NSE price source) must precede
    # investment_closing_prices (Yahoo fallback), and +-60min jitter could
    # let them race (see a025a1a, the commit that added the jitter this
    # spec removes). 23:00 UTC is the binding floor for
    # investment_closing_prices -- ~1h margin past the worst-case (EST)
    # US market close settle. See docs/specs/spec-089-daily-job-schedule-ist-morning.md.
    register_daily_job(
        export_cleanup_job,
        job_id="export_cleanup",
        hour_utc=21,
        minute_utc=30,
    )
    register_daily_job(
        session_cleanup_job,
        job_id="session_cleanup",
        hour_utc=21,
        minute_utc=45,
    )
    register_daily_job(
        import_preview_cleanup_job,
        job_id="import_preview_cleanup",
        hour_utc=22,
        minute_utc=0,
    )
    register_daily_job(
        fx_rate_ingestion_job,
        job_id="fx_rate_ingestion",
        hour_utc=22,
        minute_utc=15,
    )
    register_daily_job(
        recurring_transactions_job,
        job_id="recurring_transactions",
        hour_utc=settings.RECURRING_TXN_GENERATION_HOUR,
        minute_utc=30,
    )
    register_daily_job(
        bhavcopy_price_feed_job,
        job_id="bhavcopy_price_feed",
        hour_utc=22,
        minute_utc=45,
    )
    register_daily_job(
        investment_closing_prices_job,
        job_id="investment_closing_prices",
        hour_utc=23,
        minute_utc=0,
    )
    register_daily_job(
        net_worth_snapshot_job,
        job_id="net_worth_snapshot",
        hour_utc=23,
        minute_utc=15,
    )
    register_daily_job(
        dashboard_insights_job,
        job_id="dashboard_insights",
        hour_utc=23,
        minute_utc=30,
    )
    register_interval_job(
        push_delivery_job,
        job_id="push_delivery",
        minutes=settings.PUSH_DELIVERY_INTERVAL_MINUTES,
        idempotent=True,
    )
    register_interval_job(
        email_delivery_job,
        job_id="email_delivery",
        minutes=settings.EMAIL_DELIVERY_INTERVAL_MINUTES,
        idempotent=True,
    )
    register_interval_job(
        todo_reminder_job,
        job_id="todo_reminder",
        minutes=settings.TODO_REMINDER_INTERVAL_MINUTES,
        idempotent=True,
    )
    register_interval_job(
        medication_reminder_job,
        job_id="medication_reminder",
        minutes=settings.HEALTH_REMINDER_INTERVAL_MINUTES,
        idempotent=True,
    )
    scheduler.add_job(
        weekly_summary_job,
        "cron",
        minute=30,
        id="weekly_summary",
        replace_existing=True,
        timezone="UTC",
        kwargs={"respect_cadence": True},
    )
    register_daily_job(
        morning_briefing_job,
        job_id="morning_briefing",
        hour_utc=settings.BRIEFING_JOB_HOUR_UTC,
        minute_utc=settings.BRIEFING_JOB_MINUTE_UTC,
    )
    # spec-089 supersedes spec-088's original 04:30 UTC digest/heartbeat
    # times (sized around the old 02:00-07:00 UTC cluster): the whole
    # chain above now finishes by 23:45 UTC, so digest/heartbeat move
    # earlier too. Digest stays fixed (never jittered) and strictly last
    # -- a jittered digest could fire before the jobs it reports on.
    register_daily_job(
        job_failure_digest_job,
        job_id="job_failure_digest",
        hour_utc=0,
        minute_utc=0,
    )
    scheduler.add_job(
        job_health_heartbeat_job,
        "cron",
        day_of_week="mon",
        hour=0,
        minute=15,
        id="job_health_heartbeat",
        replace_existing=True,
        timezone="UTC",
    )

    # Update scheduler metrics
    set_scheduler_running(True)
    set_jobs_registered(len(scheduler.get_jobs()))


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

    # Response compression
    _app.add_middleware(GZipMiddleware, minimum_size=500)

    # ETag support for conditional requests on GET list endpoints
    _app.add_middleware(
        ETagMiddleware,
        paths=[
            f"{settings.API_V1_STR}/dashboard",
            f"{settings.API_V1_STR}/finance",
            f"{settings.API_V1_STR}/summaries",
            f"{settings.API_V1_STR}/spending",
            f"{settings.API_V1_STR}/investing",
            f"{settings.API_V1_STR}/health",
            f"{settings.API_V1_STR}/todo",
        ],
        min_content_length=100,
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

    # PostHog error tracking (spec-081) — inert without POSTHOG_API_KEY.
    init_posthog()

    # OpenTelemetry traces/logs to PostHog OTLP (spec-082) — inert without
    # OTEL_EXPORTER_OTLP_ENDPOINT; installs a no-op provider otherwise.
    if settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        setup_tracing()
        setup_log_export()
        FastAPIInstrumentor.instrument_app(_app)
        SQLAlchemyInstrumentor().instrument(engine=postgres.engine.sync_engine)
        HTTPXClientInstrumentor().instrument()

    # Authentication and user injection are now handled via FastAPI Depends()
    # Security headers are handled by SecurityHeadersMiddleware

    # Include routers under /v1 prefix
    _app.include_router(auth_router, prefix=f"{settings.API_V1_STR}/auth", tags=["auth"])
    _app.include_router(todo_router, prefix=settings.API_V1_STR)
    _app.include_router(health_module_router, prefix=settings.API_V1_STR)
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

    # Mount MCP server if enabled
    if settings.MCP_ENABLED and _mcp_app:
        _app.mount(settings.MCP_MOUNT_PATH, _mcp_app)

    return _app


app = create_app()
