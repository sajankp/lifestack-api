from starlette.datastructures import MutableHeaders

from app.config import settings


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
                    csp_policy = (
                        "default-src 'self'; "
                        "img-src 'self' data:; "
                        "style-src 'self' 'unsafe-inline'; "
                        "script-src 'self'"
                    )
                    headers.append("content-security-policy", csp_policy)

                if settings.COOKIE_SECURE:
                    headers.append(
                        "strict-transport-security", "max-age=31536000; includeSubDomains"
                    )

                headers.append("x-content-type-options", "nosniff")
                headers.append("x-frame-options", "DENY")
                headers.append("referrer-policy", "strict-origin-when-cross-origin")

            await send(message)

        await self.app(scope, receive, send_wrapper)
