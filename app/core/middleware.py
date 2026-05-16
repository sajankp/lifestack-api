import time
import uuid

import structlog
from fastapi import Request, Response
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings


class StructlogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        structlog.contextvars.clear_contextvars()

        request_id = str(uuid.uuid4())

        # Bind basic request context
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            client_ip=request.client.host if request.client else "unknown",
        )

        # Attempt to extract Session ID (sid) from cookies
        sid = request.cookies.get("sid")
        if sid:
            structlog.contextvars.bind_contextvars(sid=sid)

        logger = structlog.get_logger()
        start_time = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            structlog.get_logger("middleware").exception("Request failed")
            raise

        process_time = time.perf_counter() - start_time
        status_code = response.status_code

        if status_code >= 500:
            log_fn = logger.error
        elif status_code >= 400:
            log_fn = logger.warning
        else:
            log_fn = logger.info

        log_fn(
            "Request finished",
            status_code=status_code,
            duration=process_time,
        )

        response.headers["X-Request-ID"] = request_id
        return response


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)

                path = scope.get("path", "").rstrip("/") or "/"
                is_docs = path in {
                    "/docs",
                    "/openapi.json",
                    f"{settings.API_V1_STR}/openapi.json",
                } or path.startswith("/docs/")

                if not is_docs:
                    # Configurable CSP
                    csp_img = (
                        f"img-src 'self' data: {settings.CSP_IMG_SRC}; "
                        if settings.CSP_IMG_SRC
                        else "img-src 'self' data:; "
                    )
                    csp_style = (
                        f"style-src 'self' 'unsafe-inline' {settings.CSP_STYLE_SRC}; "
                        if settings.CSP_STYLE_SRC
                        else "style-src 'self' 'unsafe-inline'; "
                    )
                    csp_script = (
                        f"script-src 'self' {settings.CSP_SCRIPT_SRC}; "
                        if settings.CSP_SCRIPT_SRC
                        else "script-src 'self'; "
                    )
                    csp_font = (
                        f"font-src 'self' {settings.CSP_FONT_SRC}; "
                        if settings.CSP_FONT_SRC
                        else "font-src 'self'; "
                    )
                    csp_connect = (
                        f"connect-src 'self' {settings.CSP_CONNECT_SRC}; "
                        if settings.CSP_CONNECT_SRC
                        else "connect-src 'self'; "
                    )

                    csp_policy = f"default-src 'self'; {csp_img}{csp_style}{csp_script}{csp_font}{csp_connect}"
                    headers["content-security-policy"] = csp_policy.strip()

                # Protocol-aware HSTS
                headers_dict = dict(scope.get("headers", []))
                # Decode headers from bytes to string for checking
                x_forwarded_proto = (
                    headers_dict.get(b"x-forwarded-proto", b"")
                    .decode("utf-8", "replace")
                    .split(",")[0]
                    .strip()
                    .lower()
                )

                if (
                    settings.COOKIE_SECURE
                    or scope.get("scheme") == "https"
                    or x_forwarded_proto == "https"
                ):
                    headers["strict-transport-security"] = "max-age=31536000; includeSubDomains"

                headers["x-content-type-options"] = "nosniff"
                headers["x-frame-options"] = "DENY"
                headers["x-xss-protection"] = "1; mode=block"
                headers["referrer-policy"] = "strict-origin-when-cross-origin"

            await send(message)

        await self.app(scope, receive, send_wrapper)
