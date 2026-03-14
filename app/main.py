import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import JSONResponse

from app.auth.router import router as auth_router
from app.config import settings
from app.core.dependencies import limiter
from app.core.exceptions import (
    APIError,
    api_exception_handler,
    http_exception_handler,
    request_validation_exception_handler,
    unhandled_exception_handler,
)
from app.spending.router import router as spending_router
from app.todo.router import router as todo_router

logger = structlog.get_logger()


def create_app() -> FastAPI:
    _app = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        openapi_url=f"{settings.API_V1_STR}/openapi.json",
        docs_url="/docs",
        redoc_url=None,
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

    # Exception Handlers
    _app.add_exception_handler(APIError, api_exception_handler)
    _app.add_exception_handler(HTTPException, http_exception_handler)
    _app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    _app.add_exception_handler(Exception, unhandled_exception_handler)
    _app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # OpenTelemetry will be initialized after the app starts if configured
    if settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        FastAPIInstrumentor.instrument_app(_app)

    from app.core.auth import get_user_info_from_token

    @_app.middleware("http")
    async def add_user_info_to_request(request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        normalized_path = request.url.path.rstrip("/") or "/"

        # Paths that do not require authentication
        public_paths = {
            f"{settings.API_V1_STR}/auth/login",
            f"{settings.API_V1_STR}/auth/register",
            f"{settings.API_V1_STR}/auth/refresh",
            f"{settings.API_V1_STR}/openapi.json",
            "/docs",
            "/openapi.json",
            "/health",
            "/",
        }

        if normalized_path in public_paths or normalized_path.startswith("/docs/"):
            return await call_next(request)

        token = request.cookies.get("access_token")

        if not token:
            auth_header = request.headers.get("Authorization")
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header.split(" ")[1]

        if not token:
            return JSONResponse(
                content={
                    "type": "about:blank",
                    "title": "Unauthorized",
                    "status": 401,
                    "detail": "Not authenticated",
                    "instance": normalized_path,
                },
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Content-Type": "application/problem+json"},
            )

        try:
            username, user_id, sid = get_user_info_from_token(token)
            str_user_id = str(user_id)
            request.state.user_id = int(str_user_id) if str_user_id.isdigit() else user_id
            request.state.username = username
            request.state.sid = sid
            # Optionally add CSRF check here if needed

            return await call_next(request)
        except HTTPException as e:
            return JSONResponse(
                content={
                    "type": "about:blank",
                    "title": "Unauthorized",
                    "status": e.status_code,
                    "detail": str(e.detail),
                    "instance": normalized_path,
                },
                status_code=e.status_code,
                headers={"WWW-Authenticate": "Bearer", "Content-Type": "application/problem+json"},
            )

    # Include routers under /v1 prefix
    _app.include_router(auth_router, prefix=f"{settings.API_V1_STR}/auth", tags=["auth"])
    _app.include_router(todo_router, prefix=settings.API_V1_STR)
    _app.include_router(spending_router, prefix=settings.API_V1_STR)

    @_app.get("/health", tags=["health"])
    async def health_check():
        """Basic health check endpoint."""
        return {"status": "ok", "version": settings.VERSION}

    return _app


app = create_app()
