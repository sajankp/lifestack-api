import json
import time
import uuid

import structlog
from fastapi import Request, Response
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings


class RequestBodyTooLargeError(Exception):
    pass


class MultipartBodySizeLimitMiddleware:
    def __init__(self, app, max_body_bytes: int):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers", []))
        content_type = headers.get(b"content-type", b"").decode("latin1").lower()
        if not content_type.startswith("multipart/form-data"):
            return await self.app(scope, receive, send)

        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                length = int(content_length.decode("latin1"))
            except ValueError:
                length = 0
            if length > self.max_body_bytes:
                return await self._send_too_large(send)

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise RequestBodyTooLargeError
            return message

        async def send_wrapper(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, send_wrapper)
        except RequestBodyTooLargeError:
            if response_started:
                raise
            await self._send_too_large(send)

    async def _send_too_large(self, send):
        body = json.dumps({
            "type": "https://lifestack.app/errors/request-too-large",
            "title": "Request Too Large",
            "status": 413,
            "detail": f"Multipart request exceeds the maximum allowed limit of {self.max_body_bytes // (1024 * 1024)}MB.",
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(body)).encode("latin1")),
            ],
        })
        await send({"type": "http.response.body", "body": body})


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
                else:
                    # Fallback CSP for FastAPI docs / Swagger UI
                    headers["content-security-policy"] = (
                        "default-src 'self'; "
                        "img-src 'self' data: https://fastapi.tiangolo.com; "
                        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                        "connect-src 'self';"
                    )

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

                client = scope.get("client")
                client_ip = client[0] if client else None
                is_trusted_proxy = client_ip in settings.TRUSTED_PROXIES if client_ip else False

                has_https_forwarded = False
                if is_trusted_proxy and x_forwarded_proto == "https":
                    has_https_forwarded = True

                if settings.COOKIE_SECURE or scope.get("scheme") == "https" or has_https_forwarded:
                    headers["strict-transport-security"] = "max-age=31536000; includeSubDomains"

                headers["x-content-type-options"] = "nosniff"
                headers["x-frame-options"] = "DENY"
                headers["x-xss-protection"] = "1; mode=block"
                headers["referrer-policy"] = "strict-origin-when-cross-origin"

            await send(message)

        await self.app(scope, receive, send_wrapper)
