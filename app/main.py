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

from app.application.jobs import budget_guardrails_job
from app.auth.router import router as auth_router
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
from app.core.middleware import SecurityHeadersMiddleware, StructlogMiddleware
from app.core.scheduler import scheduler, shutdown_scheduler, start_scheduler
from app.dashboard.router import router as dashboard_router
from app.spending.router import router as spending_router
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await _startup_check()
    if settings.SCHEDULER_ENABLED:
        scheduler.add_job(
            budget_guardrails_job,
            "interval",
            hours=settings.BUDGET_GUARDRAILS_INTERVAL_HOURS,
            id="budget_guardrails",
            replace_existing=True,
        )
        start_scheduler()
    yield
    if settings.SCHEDULER_ENABLED:
        shutdown_scheduler()


def create_app() -> FastAPI:
    _app = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        openapi_url=f"{settings.API_V1_STR}/openapi.json",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    # CORS
    cors_origins = settings.cors_allowed_origins
    if cors_origins:
        _app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Core middlewares
    _app.state.limiter = limiter
    _app.add_middleware(SlowAPIMiddleware)
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
    _app.include_router(dashboard_router, prefix=settings.API_V1_STR)

    _app.include_router(health_router)

    return _app


app = create_app()
