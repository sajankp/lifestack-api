import secrets

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import JSONResponse
from starlette.status import HTTP_401_UNAUTHORIZED

from app.auth.repository import AuthSessionRepository
from app.auth.router import router as auth_router
from app.config import settings
from app.core.auth import get_user_info_from_token
from app.core.database import postgres
from app.core.dependencies import limiter
from app.core.exceptions import (
    PROBLEM_JSON,
    APIError,
    CSRFFailedError,
    UnauthorizedError,
    _type_uri_for_status,
    api_exception_handler,
    http_exception_handler,
    rate_limit_exception_handler,
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
    _app.add_exception_handler(RateLimitExceeded, rate_limit_exception_handler)

    # OpenTelemetry will be initialized after the app starts if configured
    if settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        FastAPIInstrumentor.instrument_app(_app)

    @_app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        normalized = request.url.path.rstrip("/") or "/"
        is_docs = normalized in {"/docs", "/openapi.json"} or normalized.startswith("/docs/")
        if not is_docs:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'"
            )
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

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
        token_from_cookie = token is not None

        if not token:
            auth_header = request.headers.get("Authorization")
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header.split(" ")[1]

        if not token:
            return JSONResponse(
                content={
                    "type": _type_uri_for_status(401),
                    "title": "Unauthorized",
                    "status": 401,
                    "detail": "Not authenticated",
                    "instance": normalized_path,
                },
                status_code=401,
                media_type=PROBLEM_JSON,
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            username, user_id, sid = get_user_info_from_token(token)
            str_user_id = str(user_id)
            request.state.user_id = int(str_user_id) if str_user_id.isdigit() else user_id
            request.state.username = username
            request.state.sid = sid

            if token_from_cookie and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                origin = request.headers.get("Origin")
                if origin:
                    try:
                        normalized_origin = settings._normalize_origin(origin)
                    except ValueError:
                        return await api_exception_handler(
                            request, CSRFFailedError(detail="Origin header is invalid")
                        )

                    if (
                        not settings.csrf_trusted_origins
                        or normalized_origin not in settings.csrf_trusted_origins
                    ):
                        return await api_exception_handler(
                            request,
                            CSRFFailedError(
                                detail="Origin is not allowed for cookie-authenticated requests"
                            ),
                        )

            async with postgres.async_session_maker() as session:
                auth_session = await AuthSessionRepository(session).get_active_by_sid(
                    sid, request.state.user_id
                )
            if not auth_session:
                return await api_exception_handler(
                    request, UnauthorizedError(detail="Session is no longer active")
                )

            return await call_next(request)
        except (HTTPException, APIError) as e:
            if isinstance(e, APIError):
                status_code = e.status_code
                detail = e.detail
                type_uri = e.type_uri
            else:
                status_code = e.status_code
                detail = str(e.detail)
                type_uri = _type_uri_for_status(e.status_code)
            return JSONResponse(
                content={
                    "type": type_uri,
                    "title": "Unauthorized",
                    "status": status_code,
                    "detail": detail,
                    "instance": normalized_path,
                },
                status_code=status_code,
                media_type=PROBLEM_JSON,
                headers={"WWW-Authenticate": "Bearer"},
            )

    # Include routers under /v1 prefix
    _app.include_router(auth_router, prefix=f"{settings.API_V1_STR}/auth", tags=["auth"])
    _app.include_router(todo_router, prefix=settings.API_V1_STR)
    _app.include_router(spending_router, prefix=settings.API_V1_STR)

    @_app.get("/health", tags=["health"])
    async def health_check():
        """Basic health check endpoint."""
        return {"status": "ok", "version": settings.VERSION}

    security = HTTPBearer()

    def verify_metrics_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
        expected_token = settings.METRICS_TOKEN
        if not secrets.compare_digest(credentials.credentials, expected_token):
            raise HTTPException(
                status_code=HTTP_401_UNAUTHORIZED,
                detail="Invalid metrics token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return credentials.credentials

    @_app.get("/metrics", tags=["health"])
    async def metrics_endpoint(token: str = Depends(verify_metrics_token)):
        """Prometheus metrics endpoint."""
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return _app


app = create_app()
