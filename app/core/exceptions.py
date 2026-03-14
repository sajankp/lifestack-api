import structlog
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = structlog.get_logger()


class APIError(Exception):
    def __init__(self, type_str: str, title: str, status_code: int, detail: str):
        self.type_str = type_str
        self.title = title
        self.status_code = status_code
        self.detail = detail


async def api_exception_handler(request: Request, exc: APIError) -> JSONResponse:
    logger.warning("api_exception", status=exc.status_code, detail=exc.detail, url=str(request.url))
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "type": f"https://lifestack.app/errors/{exc.type_str}",
            "title": exc.title,
            "status": exc.status_code,
            "detail": exc.detail,
            "instance": str(request.url.path),
        },
    )


async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    logger.warning("validation_error", detail=exc.errors(), url=str(request.url))
    return JSONResponse(
        status_code=422,
        content={
            "type": "https://lifestack.app/errors/validation-error",
            "title": "Request Validation Error",
            "status": 422,
            "detail": "The request payload or parameters are invalid.",
            "instance": str(request.url.path),
            "errors": exc.errors(),
        },
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_exception", error=str(exc), url=str(request.url))
    return JSONResponse(
        status_code=500,
        content={
            "type": "https://lifestack.app/errors/internal-server-error",
            "title": "Internal Server Error",
            "status": 500,
            "detail": "An unexpected error occurred processing your request.",
            "instance": str(request.url.path),
        },
    )
