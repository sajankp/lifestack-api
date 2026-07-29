import pytest
from fastapi import Request, Response

from app.core.etag import ETagMiddleware, generate_etag
from app.core.middleware import MultipartBodySizeLimitMiddleware, _format_size_limit


def test_format_size_limit_uses_human_readable_units():
    assert _format_size_limit(512) == "512B"
    assert _format_size_limit(1536) == "1.5KB"
    assert _format_size_limit(10 * 1024 * 1024) == "10MB"


@pytest.mark.asyncio
async def test_multipart_limiter_suppresses_downstream_disconnect_after_rejection():
    async def app(scope, receive, send):
        message = await receive()
        assert message["type"] == "http.disconnect"
        raise RuntimeError("downstream saw client disconnect after 413")

    middleware = MultipartBodySizeLimitMiddleware(app, max_body_bytes=4)
    sent_messages = []

    async def receive():
        return {"type": "http.request", "body": b"too large", "more_body": False}

    async def send(message):
        sent_messages.append(message)

    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/imports",
            "headers": [(b"content-type", b"multipart/form-data; boundary=x")],
        },
        receive,
        send,
    )

    assert sent_messages[0]["type"] == "http.response.start"
    assert sent_messages[0]["status"] == 413


@pytest.mark.asyncio
async def test_multipart_limiter_does_not_send_413_after_response_started():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        message = await receive()
        assert message["type"] == "http.disconnect"
        await send({"type": "http.response.body", "body": b"accepted"})

    middleware = MultipartBodySizeLimitMiddleware(app, max_body_bytes=4)
    sent_messages = []

    async def receive():
        return {"type": "http.request", "body": b"too large", "more_body": False}

    async def send(message):
        sent_messages.append(message)

    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/imports",
            "headers": [(b"content-type", b"multipart/form-data; boundary=x")],
        },
        receive,
        send,
    )

    assert [message["type"] for message in sent_messages] == [
        "http.response.start",
        "http.response.body",
    ]
    assert sent_messages[0]["status"] == 200


@pytest.mark.asyncio
async def test_etag_middleware_preserves_cors_headers_on_304():
    body = b'{"entries":[],"latest_kg":null}'
    etag = generate_etag(body)

    async def inner_app(scope, receive, send):
        # Simulate downstream app returning a 200 response with CORS headers attached by CORSMiddleware
        response = Response(
            content=body,
            status_code=200,
            headers={
                "content-type": "application/json",
                "access-control-allow-origin": "https://lifestack.sajankp.com",
                "access-control-allow-credentials": "true",
            },
        )
        await response(scope, receive, send)

    middleware = ETagMiddleware(inner_app, paths=["/v1/health"])

    # Create request with matching If-None-Match
    req = Request(
        scope={
            "type": "http",
            "method": "GET",
            "path": "/v1/health/weight/trend",
            "headers": [
                (b"if-none-match", etag.encode("utf-8")),
                (b"origin", b"https://lifestack.sajankp.com"),
            ],
        }
    )

    async def call_next(r):
        response = Response(
            content=body,
            status_code=200,
            headers={
                "content-type": "application/json",
                "access-control-allow-origin": "https://lifestack.sajankp.com",
                "access-control-allow-credentials": "true",
            },
        )
        return response

    res = await middleware.dispatch(req, call_next)
    assert res.status_code == 304
    assert res.headers["ETag"] == etag
    assert res.headers["access-control-allow-origin"] == "https://lifestack.sajankp.com"
    assert res.headers["access-control-allow-credentials"] == "true"
