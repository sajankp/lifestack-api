"""spec-110: request-latency histogram, slow-query log, voice tool histogram."""

import httpx
import pytest
import structlog
from fastapi import FastAPI
from prometheus_client import REGISTRY
from sqlalchemy import create_engine, text

from app.capture.agent import _declared_tool_names as declared_tool_names
from app.core.database.slow_query import install_slow_query_logging
from app.core.middleware import StructlogMiddleware
from app.observability.request_metrics import (
    observe_capture_tool,
    route_label,
)


def _sample(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(StructlogMiddleware)

    @app.get("/v1/items/{item_id}")
    async def item(item_id: int):
        return {"id": item_id}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(path)


COUNT = "lifestack_http_request_duration_seconds_count"


@pytest.mark.asyncio
async def test_request_is_observed_by_route_template_not_raw_path():
    labels = {"method": "GET", "route": "/v1/items/{item_id}", "status_class": "2xx"}
    before = _sample(COUNT, labels)

    await _get(_app(), "/v1/items/123")
    await _get(_app(), "/v1/items/456")

    assert _sample(COUNT, labels) == before + 2
    # the raw ids must never become a label value
    assert _sample(COUNT, {**labels, "route": "/v1/items/123"}) == 0.0


@pytest.mark.asyncio
async def test_unmatched_paths_share_one_series():
    labels = {"method": "GET", "route": "unmatched", "status_class": "4xx"}
    before = _sample(COUNT, labels)

    await _get(_app(), "/wp-admin/setup.php")
    await _get(_app(), "/.env")

    assert _sample(COUNT, labels) == before + 2


@pytest.mark.asyncio
async def test_health_is_not_observed():
    labels = {"method": "GET", "route": "/health", "status_class": "2xx"}
    before = _sample(COUNT, labels)

    await _get(_app(), "/health")

    assert _sample(COUNT, labels) == before


@pytest.mark.asyncio
async def test_request_finished_log_line_carries_the_route_template():
    with structlog.testing.capture_logs() as logs:
        await _get(_app(), "/v1/items/9")

    finished = [e for e in logs if e.get("event") == "Request finished"]
    assert finished and finished[0]["route"] == "/v1/items/{item_id}"


def test_route_label_rejects_nonstandard_methods_and_unknown_routes():
    class Req:
        def __init__(self, method, scope):
            self.method = method
            self.scope = scope

    assert route_label(Req("GET", {})) == ("GET", "unmatched")
    assert route_label(Req("FROBNICATE", {})) == ("OTHER", "unmatched")


# ── slow-query logging ──────────────────────────────────────────────────────


def _engine():
    return create_engine("sqlite://")


def test_slow_query_is_logged_without_bind_parameters_and_counted():
    engine = _engine()
    install_slow_query_logging(engine, threshold_ms=0.0001)
    before = _sample("lifestack_db_slow_queries_total", {})

    with structlog.testing.capture_logs() as logs, engine.connect() as conn:
        conn.execute(text("select :secret as v"), {"secret": "hunter2-amount-9999"})

    slow = [e for e in logs if e.get("event") == "slow_query"]
    assert slow
    assert "select" in slow[0]["statement"].lower()
    assert slow[0]["duration_ms"] >= 0
    assert "hunter2" not in repr(slow)  # parameters are never logged
    assert _sample("lifestack_db_slow_queries_total", {}) == before + len(slow)


def test_fast_query_is_not_logged():
    engine = _engine()
    install_slow_query_logging(engine, threshold_ms=60_000)

    with structlog.testing.capture_logs() as logs, engine.connect() as conn:
        conn.execute(text("select 1"))

    assert not [e for e in logs if e.get("event") == "slow_query"]


def test_threshold_zero_disables_logging():
    engine = _engine()
    install_slow_query_logging(engine, threshold_ms=0)

    with structlog.testing.capture_logs() as logs, engine.connect() as conn:
        conn.execute(text("select 1"))

    assert not [e for e in logs if e.get("event") == "slow_query"]


def test_long_statements_are_collapsed_and_truncated():
    engine = _engine()
    install_slow_query_logging(engine, threshold_ms=0.0001)
    long_sql = "select 1 as a,\n" + ",\n".join(f"{i} as c{i}" for i in range(200))

    with structlog.testing.capture_logs() as logs, engine.connect() as conn:
        conn.execute(text(long_sql))

    stmt = [e for e in logs if e.get("event") == "slow_query"][0]["statement"]
    assert "\n" not in stmt and len(stmt) <= 303  # 300 + ellipsis


# ── voice tool histogram ────────────────────────────────────────────────────

TOOL_COUNT = "lifestack_capture_tool_duration_seconds_count"


def test_agent_known_tools_come_from_the_declared_tool_set():
    names = declared_tool_names()
    assert {"list_todos", "log_spending_transaction", "create_transfer"} <= names


def test_tool_histogram_uses_declared_tool_names_only():
    before = _sample(TOOL_COUNT, {"tool": "list_todos", "status": "success"})
    before_other = _sample(TOOL_COUNT, {"tool": "other", "status": "success"})

    known = frozenset({"list_todos"})
    observe_capture_tool("list_todos", "success", 0.25, known_tools=known)
    observe_capture_tool("model_invented_tool_xyz", "success", 0.25, known_tools=known)

    assert _sample(TOOL_COUNT, {"tool": "list_todos", "status": "success"}) == before + 1
    assert _sample(TOOL_COUNT, {"tool": "other", "status": "success"}) == before_other + 1
    assert _sample(TOOL_COUNT, {"tool": "model_invented_tool_xyz", "status": "success"}) == 0.0
