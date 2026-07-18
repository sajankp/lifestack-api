"""ETag generation and conditional request handling for GET list endpoints."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from functools import wraps

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp


def generate_etag(data: dict | list | str | bytes | BaseModel) -> str:
    """Generate a strong ETag from response data using SHA-256.

    Accepts raw ``bytes`` directly to avoid JSON decode/re-encode overhead in
    the middleware path where the response body is already available as bytes.
    """
    if isinstance(data, bytes):
        content_bytes = data
    elif isinstance(data, BaseModel):
        content_bytes = json.dumps(
            data.model_dump(mode="json"), sort_keys=True, default=str
        ).encode("utf-8")
    elif isinstance(data, str):
        content_bytes = data.encode("utf-8")
    else:
        # Use JSON serialization with sorted keys for consistent hashing
        content_bytes = json.dumps(data, sort_keys=True, default=str).encode("utf-8")

    # Generate SHA-256 hash and truncate to 32 chars for reasonable header size
    etag = hashlib.sha256(content_bytes).hexdigest()[:32]
    return f'W/"{etag}"'  # Weak ETag since content may be semantically equivalent but not byte-identical


def generate_etag_from_model(model: BaseModel) -> str:
    """Generate ETag from a Pydantic model."""
    return generate_etag(model.model_dump(mode="json"))


class ETagMiddleware(BaseHTTPMiddleware):
    """
    Middleware that adds ETag headers to GET responses and handles If-None-Match requests.

    Only applies to successful GET responses with JSON content.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        paths: list[str] | None = None,
        min_content_length: int = 0,
    ):
        super().__init__(app)
        self.paths = paths or []
        self.min_content_length = min_content_length

    def _should_process(self, request: Request, response: Response) -> bool:
        """Determine if we should add ETag to this response."""
        # Only process GET requests
        if request.method != "GET":
            return False

        # Only process successful responses
        if response.status_code < 200 or response.status_code >= 300:
            return False

        # Only process JSON responses
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            return False

        # Check path filtering
        if self.paths:
            path = request.url.path
            if not any(path.startswith(p) for p in self.paths):
                return False

        return True

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Check If-None-Match header for conditional requests
        if_none_match = request.headers.get("if-none-match")

        response = await call_next(request)

        if not self._should_process(request, response):
            return response

        # Read response body
        if hasattr(response, "body"):
            body = response.body
        else:
            # Streaming response - collect body
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
            # Create new response with collected body
            response = Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        if len(body) < self.min_content_length:
            return response

        try:
            # Hash raw bytes directly — avoids json.loads + re-encode overhead on every GET
            etag = generate_etag(body)

            # Check If-None-Match
            if if_none_match and if_none_match == etag:
                return Response(status_code=304, headers={"ETag": etag})

            # Add ETag header
            response.headers["ETag"] = etag
            return response

        except Exception:
            # Unexpected error hashing body — return original response without ETag
            return response


def etag_response(model_or_data: dict | list | BaseModel) -> str:
    """Helper to generate ETag from a model or data dict."""
    if hasattr(model_or_data, "model_dump"):
        return generate_etag_from_model(model_or_data)
    return generate_etag(model_or_data)


class ConditionalRequest:
    """Dependency for handling conditional GET requests in endpoints."""

    def __init__(self, request: Request):
        self.request = request
        self.if_none_match = request.headers.get("if-none-match")
        self.if_modified_since = request.headers.get("if-modified-since")

    def check_etag(self, etag: str) -> bool:
        """Check if the If-None-Match header matches the given ETag."""
        return self.if_none_match is not None and self.if_none_match == etag

    def check_modified_since(self, last_modified: str) -> bool:
        """Check if the If-Modified-Since header indicates content hasn't changed."""
        # This is a simplified check - in practice you'd parse the HTTP date
        return False  # Not implemented yet

    def not_modified_response(self, etag: str) -> JSONResponse:
        """Return a 304 Not Modified response."""
        return JSONResponse(
            status_code=304,
            content=None,
            headers={"ETag": etag},
        )


# Decorator for easy ETag handling in endpoint functions
def with_etag(
    generate_fn: Callable,
    *,
    key_params: list[str] | None = None,
):
    """
    Decorator to add ETag support to an endpoint.

    Usage:
        @router.get("/items", response_model=PaginatedResponse[ItemResponse])
        @with_etag(lambda items, total, pagination: generate_etag({"items": items, "total": total}))
        async def list_items(...):
            items, total = await service.list_items(...)
            return build_page(items, total, pagination)
    """

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Get the request object from kwargs (FastAPI injects it)
            request = kwargs.get("request")
            if not request:
                # Find request in args
                for arg in args:
                    if isinstance(arg, Request):
                        request = arg
                        break

            # Call the original function
            result = await func(*args, **kwargs)

            # Generate ETag from result
            if generate_fn:
                etag = generate_fn(result)
            elif key_params and hasattr(result, "model_dump"):
                data = {k: getattr(result, k) for k in key_params if hasattr(result, k)}
                etag = generate_etag(data)
            elif hasattr(result, "model_dump"):
                etag = generate_etag(result.model_dump(mode="json"))
            else:
                etag = generate_etag(result)

            # Check If-None-Match
            if_none_match = request.headers.get("if-none-match") if request else None
            if if_none_match and if_none_match == etag:
                return Response(status_code=304, headers={"ETag": etag})

            # Set ETag on the FastAPI-injected Response object (standard FastAPI pattern for
            # header injection when the endpoint returns a Pydantic model/dict, not a Response)
            injected_response = kwargs.get("response")
            if isinstance(injected_response, Response):
                injected_response.headers["ETag"] = etag
            elif isinstance(result, Response):
                result.headers["ETag"] = etag

            return result

        return wrapper

    return decorator
