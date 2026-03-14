import structlog
from fastapi import Request
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
