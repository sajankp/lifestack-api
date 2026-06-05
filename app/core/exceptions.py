import structlog
from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.config import settings

logger = structlog.get_logger()

PROBLEM_JSON = "application/problem+json"
ERROR_BASE_URI = "https://lifestack.app/errors"

_STATUS_TYPE_MAP: dict[int, str] = {
    400: "bad-request",
    401: "unauthorized",
    403: "forbidden",
    404: "not-found",
    405: "method-not-allowed",
    409: "conflict",
    422: "validation-error",
    429: "rate-limit-exceeded",
}

_STATUS_HINT_MAP: dict[int, str] = {
    400: "Review request parameters and try again.",
    401: "Authenticate and retry the request.",
    403: "Verify workspace access and security constraints.",
    404: "Confirm the resource exists in the current workspace.",
    405: "Use a supported HTTP method for this endpoint.",
    409: "Resolve conflicting state and retry.",
    422: "Fix invalid fields and retry the request.",
    429: "Wait and retry after the rate limit window.",
    500: "Retry later or contact support if the problem persists.",
}


def _type_uri_for_status(status_code: int) -> str:
    """Map an HTTP status code to its canonical RFC 7807 type URI."""
    slug = _STATUS_TYPE_MAP.get(status_code, "api-error")
    return f"{ERROR_BASE_URI}/{slug}"


def _code_from_type_uri(type_uri: str) -> str:
    slug = type_uri.rsplit("/", maxsplit=1)[-1] if "/" in type_uri else type_uri
    return slug.replace("-", "_")


def _hint_for_status(status_code: int) -> str:
    return _STATUS_HINT_MAP.get(status_code, "Review the request and try again.")


# ---------------------------------------------------------------------------
# Exception hierarchy — subclass to get automatic type/title/status defaults
# ---------------------------------------------------------------------------


class APIError(Exception):
    """Base RFC 7807 exception. All domain errors inherit from this.

    Subclasses set class-level defaults for type_str, title, and status_code.
    Call sites only need to provide ``detail``.
    """

    type_str: str = "internal-server-error"
    title: str = "Internal Server Error"
    status_code: int = 500

    def __init__(
        self,
        detail: str,
        *,
        type_str: str | None = None,
        title: str | None = None,
        status_code: int | None = None,
        **extra_fields: object,
    ):
        if type_str is not None:
            self.type_str = type_str
        if title is not None:
            self.title = title
        if status_code is not None:
            self.status_code = status_code
        self.detail = detail
        self.extra_fields = extra_fields

    @property
    def type_uri(self) -> str:
        return f"{ERROR_BASE_URI}/{self.type_str}"


class NotFoundError(APIError):
    type_str = "not-found"
    title = "Not Found"
    status_code = 404


class UnauthorizedError(APIError):
    type_str = "unauthorized"
    title = "Unauthorized"
    status_code = 401


class ForbiddenError(APIError):
    type_str = "forbidden"
    title = "Forbidden"
    status_code = 403


class ConflictError(APIError):
    type_str = "conflict"
    title = "Conflict"
    status_code = 409


class ValidationError(APIError):
    type_str = "validation-error"
    title = "Validation Error"
    status_code = 422


class RateLimitError(APIError):
    type_str = "rate-limit-exceeded"
    title = "Rate Limit Exceeded"
    status_code = 429


class CSRFFailedError(APIError):
    type_str = "csrf-check-failed"
    title = "CSRF Check Failed"
    status_code = 403


# --- Module-specific subclasses ---


class CategoryInUseError(ConflictError):
    """Spending module: category has transactions referencing it."""

    type_str = "category-in-use"
    title = "Category In Use"


# ---------------------------------------------------------------------------
# Exception handlers — every handler enforces application/problem+json
# ---------------------------------------------------------------------------


def _add_cors_headers(request: Request, response: JSONResponse) -> JSONResponse:
    origin = request.headers.get("origin")
    if origin and settings.cors_allowed_origins:
        try:
            normalized_origin = settings._normalize_origin(origin)
        except ValueError:
            normalized_origin = None

        if (
            "*" in settings.cors_allowed_origins
            or normalized_origin in settings.cors_allowed_origins
        ):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = (
                "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            )
            response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, Authorization, X-Requested-With, X-Request-ID, X-CSRF-Token, Origin, Accept"
            )
    return response


async def api_exception_handler(request: Request, exc: APIError) -> JSONResponse:
    """Handle all APIError subclasses with RFC 7807 problem details."""
    logger.warning(
        "api_exception",
        type=exc.type_str,
        status=exc.status_code,
        detail=exc.detail,
        url=str(request.url),
    )
    body: dict[str, object] = {
        "type": exc.type_uri,
        "code": _code_from_type_uri(exc.type_uri),
        "title": exc.title,
        "status": exc.status_code,
        "detail": exc.detail,
        "hint": _hint_for_status(exc.status_code),
        "instance": str(request.url.path),
    }
    if exc.extra_fields:
        body.update(exc.extra_fields)
    return _add_cors_headers(
        request,
        JSONResponse(
            status_code=exc.status_code,
            content=body,
            media_type=PROBLEM_JSON,
        ),
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Handle FastAPI HTTPException with proper type URIs (no about:blank)."""
    logger.warning(
        "http_exception", status=exc.status_code, detail=exc.detail, url=str(request.url)
    )
    type_uri = _type_uri_for_status(exc.status_code)
    return _add_cors_headers(
        request,
        JSONResponse(
            status_code=exc.status_code,
            content={
                "type": type_uri,
                "code": _code_from_type_uri(type_uri),
                "title": exc.detail if isinstance(exc.detail, str) else "API Error",
                "status": exc.status_code,
                "detail": str(exc.detail),
                "hint": _hint_for_status(exc.status_code),
                "instance": str(request.url.path),
            },
            media_type=PROBLEM_JSON,
        ),
    )


async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    logger.warning("validation_error", errors=jsonable_encoder(exc.errors()), url=str(request.url))
    return _add_cors_headers(
        request,
        JSONResponse(
            status_code=422,
            content={
                "type": f"{ERROR_BASE_URI}/validation-error",
                "code": "validation_error",
                "title": "Request Validation Error",
                "status": 422,
                "detail": "The request payload or parameters are invalid.",
                "hint": _hint_for_status(422),
                "instance": str(request.url.path),
                "errors": jsonable_encoder(exc.errors()),
            },
            media_type=PROBLEM_JSON,
        ),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_exception", error=str(exc), url=str(request.url))
    return _add_cors_headers(
        request,
        JSONResponse(
            status_code=500,
            content={
                "type": f"{ERROR_BASE_URI}/internal-server-error",
                "code": "internal_server_error",
                "title": "Internal Server Error",
                "status": 500,
                "detail": "An unexpected error occurred processing your request.",
                "hint": _hint_for_status(500),
                "instance": str(request.url.path),
            },
            media_type=PROBLEM_JSON,
        ),
    )


async def rate_limit_exception_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    logger.warning("rate_limit_exceeded", detail=exc.detail, url=str(request.url))
    response = JSONResponse(
        status_code=429,
        content={
            "type": f"{ERROR_BASE_URI}/rate-limit-exceeded",
            "code": "rate_limit_exceeded",
            "title": "Rate Limit Exceeded",
            "status": 429,
            "detail": f"Rate limit exceeded: {exc.detail}",
            "hint": _hint_for_status(429),
            "instance": str(request.url.path),
        },
        media_type=PROBLEM_JSON,
    )
    if hasattr(request.state, "view_rate_limit"):
        response = request.app.state.limiter._inject_headers(
            response, request.state.view_rate_limit
        )
    return _add_cors_headers(request, response)
