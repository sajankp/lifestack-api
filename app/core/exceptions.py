import structlog
from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

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


def _type_uri_for_status(status_code: int) -> str:
    """Map an HTTP status code to its canonical RFC 7807 type URI."""
    slug = _STATUS_TYPE_MAP.get(status_code, "api-error")
    return f"{ERROR_BASE_URI}/{slug}"


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
        "title": exc.title,
        "status": exc.status_code,
        "detail": exc.detail,
        "instance": str(request.url.path),
    }
    if exc.extra_fields:
        body.update(exc.extra_fields)
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        media_type=PROBLEM_JSON,
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Handle FastAPI HTTPException with proper type URIs (no about:blank)."""
    logger.warning(
        "http_exception", status=exc.status_code, detail=exc.detail, url=str(request.url)
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "type": _type_uri_for_status(exc.status_code),
            "title": exc.detail if isinstance(exc.detail, str) else "API Error",
            "status": exc.status_code,
            "detail": str(exc.detail),
            "instance": str(request.url.path),
        },
        media_type=PROBLEM_JSON,
    )


async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    logger.warning("validation_error", detail=exc.errors(), url=str(request.url))
    return JSONResponse(
        status_code=422,
        content={
            "type": f"{ERROR_BASE_URI}/validation-error",
            "title": "Request Validation Error",
            "status": 422,
            "detail": "The request payload or parameters are invalid.",
            "instance": str(request.url.path),
            "errors": exc.errors(),
        },
        media_type=PROBLEM_JSON,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_exception", error=str(exc), url=str(request.url))
    return JSONResponse(
        status_code=500,
        content={
            "type": f"{ERROR_BASE_URI}/internal-server-error",
            "title": "Internal Server Error",
            "status": 500,
            "detail": "An unexpected error occurred processing your request.",
            "instance": str(request.url.path),
        },
        media_type=PROBLEM_JSON,
    )


async def rate_limit_exception_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    logger.warning("rate_limit_exceeded", detail=exc.detail, url=str(request.url))
    response = JSONResponse(
        status_code=429,
        content={
            "type": f"{ERROR_BASE_URI}/rate-limit-exceeded",
            "title": "Rate Limit Exceeded",
            "status": 429,
            "detail": f"Rate limit exceeded: {exc.detail}",
            "instance": str(request.url.path),
        },
        media_type=PROBLEM_JSON,
    )
    if hasattr(request.state, "view_rate_limit"):
        response = request.app.state.limiter._inject_headers(
            response, request.state.view_rate_limit
        )
    return response
