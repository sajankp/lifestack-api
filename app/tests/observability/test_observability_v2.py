"""spec-112: slow-query visibility, request DB attribution, pool/host gauges, MCP tool metrics,
pg_stat_statements report, prod compose logging."""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import structlog
import yaml
from fastapi import FastAPI
from prometheus_client import REGISTRY, CollectorRegistry
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool, QueuePool

from app.config import settings
from app.core import logging as app_logging
from app.core.database.slow_query import install_slow_query_logging
from app.core.middleware import StructlogMiddleware
from app.mcp.server import create_mcp_server, registered_tool_names
from app.observability.db_profile import begin_request_profile, end_request_profile
from app.observability.host_metrics import HostMetricsCollector, register_host_metrics
from app.observability.mcp_metrics import McpToolMetricsMiddleware
from app.observability.pg_stat import StatementRow, format_report
from app.observability.pool_metrics import register_pool_metrics
from app.observability.request_metrics import route_label

REPO = Path(__file__).resolve().parents[3]


def _sample(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# ── 1. slow-query log silently suppressed by LOG_LEVEL ──────────────────────


def _guard_events(monkeypatch, level: str, threshold: float):
    monkeypatch.setattr(settings, "LOG_LEVEL", level)
    monkeypatch.setattr(settings, "SLOW_QUERY_THRESHOLD_MS", threshold)
    with structlog.testing.capture_logs() as logs:
        app_logging.warn_if_slow_query_log_suppressed()
    return [e for e in logs if e["event"] == "slow_query_log_suppressed_by_log_level"]


def test_guard_reports_when_log_level_hides_slow_queries(monkeypatch):
    events = _guard_events(monkeypatch, "ERROR", 500)
    assert len(events) == 1
    assert events[0]["log_level"] == "error"  # must itself survive the ERROR filter


@pytest.mark.parametrize(
    ("level", "threshold"),
    [("WARNING", 500), ("INFO", 500), ("DEBUG", 500), ("ERROR", 0)],
)
def test_guard_is_quiet_when_slow_queries_are_visible_or_disabled(monkeypatch, level, threshold):
    assert _guard_events(monkeypatch, level, threshold) == []


# ── 2. per-request DB profile + slow-query route label ──────────────────────

DB_QUERIES = "lifestack_http_request_db_queries"
DB_SECONDS = "lifestack_http_request_db_seconds"
SLOW = "lifestack_db_slow_queries_total"


def _db_app(engine) -> FastAPI:
    app = FastAPI()
    app.add_middleware(StructlogMiddleware)

    @app.get("/v1/work/{n}")
    async def work(n: int):
        with engine.connect() as conn:
            for _ in range(3):
                conn.execute(text("select 1"))
        return {"n": n}

    @app.get("/health")
    async def health():
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        return {"status": "ok"}

    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_request_records_query_count_and_db_time_by_route_template():
    engine = create_engine("sqlite://")
    install_slow_query_logging(engine, threshold_ms=60_000)
    labels = {"route": "/v1/work/{n}"}
    q0 = _sample(f"{DB_QUERIES}_count", labels)
    qs0 = _sample(f"{DB_QUERIES}_sum", labels)
    s0 = _sample(f"{DB_SECONDS}_count", labels)

    await _get(_db_app(engine), "/v1/work/1")
    await _get(_db_app(engine), "/v1/work/2")

    assert _sample(f"{DB_QUERIES}_count", labels) == q0 + 2
    assert _sample(f"{DB_QUERIES}_sum", labels) == qs0 + 6  # 3 queries per request
    assert _sample(f"{DB_SECONDS}_count", labels) == s0 + 2
    assert _sample(f"{DB_QUERIES}_count", {"route": "/v1/work/1"}) == 0.0  # raw path never a label


@pytest.mark.asyncio
async def test_health_probe_is_not_profiled():
    engine = create_engine("sqlite://")
    install_slow_query_logging(engine, threshold_ms=60_000)
    before = _sample(f"{DB_QUERIES}_count", {"route": "/health"})

    await _get(_db_app(engine), "/health")

    assert _sample(f"{DB_QUERIES}_count", {"route": "/health"}) == before


@pytest.mark.asyncio
async def test_slow_queries_are_counted_per_route():
    engine = create_engine("sqlite://")
    install_slow_query_logging(engine, threshold_ms=0.0001)
    labels = {"route": "/v1/work/{n}"}
    before = _sample(SLOW, labels)

    await _get(_db_app(engine), "/v1/work/1")

    assert _sample(SLOW, labels) >= before + 3


def test_slow_query_outside_a_request_uses_the_none_route():
    engine = create_engine("sqlite://")
    install_slow_query_logging(engine, threshold_ms=0.0001)
    before = _sample(SLOW, {"route": "none"})

    with engine.connect() as conn:
        conn.execute(text("select 1"))

    assert _sample(SLOW, {"route": "none"}) > before


@pytest.mark.asyncio
async def test_profile_is_visible_to_the_listener_through_an_async_engine(migrated_database_url):
    """The contextvar must survive SQLAlchemy's greenlet bridge, not just sync engines."""
    engine = create_async_engine(migrated_database_url, poolclass=NullPool)
    install_slow_query_logging(engine.sync_engine, threshold_ms=60_000)
    profile, token = begin_request_profile({"route": object()})
    try:
        async with engine.connect() as conn:
            await conn.execute(text("select 1"))
            await conn.execute(text("select 2"))
    finally:
        end_request_profile(token)
        await engine.dispose()

    assert profile.queries >= 2
    assert profile.seconds > 0


# ── 3. MCP traffic is labelled, not "unmatched" ─────────────────────────────


class _Req:
    def __init__(self, scope):
        self.method = "POST"
        self.scope = scope


def test_requests_under_the_mcp_mount_are_labelled_mcp(monkeypatch):
    monkeypatch.setattr(settings, "MCP_MOUNT_PATH", "/mcp")
    assert route_label(_Req({"path": "/mcp"})) == ("POST", "mcp")
    assert route_label(_Req({"path": "/mcp/"})) == ("POST", "mcp")
    assert route_label(_Req({"path": "/mcpx"})) == ("POST", "unmatched")  # prefix, not substring
    assert route_label(_Req({"path": "/wp-admin"})) == ("POST", "unmatched")


def test_a_matched_route_wins_over_the_mcp_prefix(monkeypatch):
    monkeypatch.setattr(settings, "MCP_MOUNT_PATH", "/mcp")
    route = SimpleNamespace(path="/mcp/status")
    assert route_label(_Req({"path": "/mcp/status", "route": route})) == ("POST", "/mcp/status")


MCP_COUNT = "lifestack_mcp_tool_duration_seconds_count"


def _mcp_ctx(name: str):
    return SimpleNamespace(message=SimpleNamespace(name=name))


async def _known():
    return {"list_todos"}


@pytest.mark.asyncio
async def test_mcp_tool_calls_are_timed_with_bounded_labels():
    mw = McpToolMetricsMiddleware(_known)
    ok = {"tool": "list_todos", "status": "success"}
    other = {"tool": "other", "status": "success"}
    b_ok, b_other = _sample(MCP_COUNT, ok), _sample(MCP_COUNT, other)

    async def call_next(_ctx):
        return "result"

    assert await mw.on_call_tool(_mcp_ctx("list_todos"), call_next) == "result"
    await mw.on_call_tool(_mcp_ctx("client_invented_name"), call_next)

    assert _sample(MCP_COUNT, ok) == b_ok + 1
    assert _sample(MCP_COUNT, other) == b_other + 1
    assert _sample(MCP_COUNT, {"tool": "client_invented_name", "status": "success"}) == 0.0


@pytest.mark.asyncio
async def test_mcp_tool_failure_is_recorded_and_reraised():
    mw = McpToolMetricsMiddleware(_known)
    labels = {"tool": "list_todos", "status": "error"}
    before = _sample(MCP_COUNT, labels)

    async def boom(_ctx):
        raise RuntimeError("tool failed")

    with pytest.raises(RuntimeError, match="tool failed"):
        await mw.on_call_tool(_mcp_ctx("list_todos"), boom)

    assert _sample(MCP_COUNT, labels) == before + 1


@pytest.mark.asyncio
async def test_real_mcp_server_has_the_middleware_and_resolves_tool_names(monkeypatch):
    monkeypatch.setattr(settings, "MCP_BASE_URL", "https://mcp.example.com")
    monkeypatch.setattr(settings, "MCP_ALLOWED_HOSTS", "mcp.example.com")
    server = create_mcp_server()

    assert any(isinstance(m, McpToolMetricsMiddleware) for m in server.middleware)
    assert len(await registered_tool_names(server)) > 10


# ── 4. pool gauges ──────────────────────────────────────────────────────────


def test_pool_gauges_report_size_checked_out_and_clamped_overflow():
    pool = QueuePool(
        lambda: sqlite3.connect(":memory:", check_same_thread=False), pool_size=3, max_overflow=2
    )
    assert register_pool_metrics(pool) is True
    conn = pool.connect()
    assert _sample("lifestack_db_pool_size", {}) == 3
    assert _sample("lifestack_db_pool_checked_out", {}) == 1
    assert _sample("lifestack_db_pool_overflow", {}) == 0  # SQLAlchemy reports -pool_size when idle
    conn.close()
    assert _sample("lifestack_db_pool_checked_out", {}) == 0


def test_pool_without_size_reporting_is_ignored_not_fatal():
    assert register_pool_metrics(NullPool(lambda: sqlite3.connect(":memory:"))) is False


# ── 5. host memory gauges (the VM's own pressure, read from /proc) ──────────

MEMINFO = """MemTotal:         977000 kB
MemFree:           70000 kB
MemAvailable:     266000 kB
SwapTotal:       6291452 kB
SwapFree:        5669000 kB
"""
PSI = """some avg10=1.25 avg60=0.50 avg300=0.10 total=12345
full avg10=0.40 avg60=0.20 avg300=0.05 total=6789
"""


def _collect(proc: Path) -> CollectorRegistry:
    registry = CollectorRegistry()
    registry.register(HostMetricsCollector(proc))
    return registry


def test_host_metrics_read_meminfo_and_pressure(tmp_path):
    (tmp_path / "meminfo").write_text(MEMINFO)
    (tmp_path / "pressure").mkdir()
    (tmp_path / "pressure" / "memory").write_text(PSI)

    reg = _collect(tmp_path)

    assert reg.get_sample_value("lifestack_host_memory_available_bytes") == 266000 * 1024
    assert reg.get_sample_value("lifestack_host_swap_used_bytes") == (6291452 - 5669000) * 1024
    assert reg.get_sample_value("lifestack_host_memory_pressure_some_avg10") == 1.25


def test_host_metrics_omit_pressure_when_the_kernel_does_not_expose_it(tmp_path):
    (tmp_path / "meminfo").write_text(MEMINFO)

    reg = _collect(tmp_path)

    assert reg.get_sample_value("lifestack_host_memory_available_bytes") is not None
    assert reg.get_sample_value("lifestack_host_memory_pressure_some_avg10") is None


def test_host_metrics_never_raise_when_proc_is_unreadable(tmp_path):
    reg = _collect(tmp_path / "does-not-exist")
    assert reg.get_sample_value("lifestack_host_memory_available_bytes") is None


def test_register_host_metrics_is_idempotent():
    register_host_metrics()
    register_host_metrics()  # a second create_app() must not raise "duplicated timeseries"


# ── 6. pg_stat_statements report ────────────────────────────────────────────


def test_report_ranks_by_total_time_with_share_and_truncates_statements():
    rows = [
        StatementRow(
            calls=1000,
            total_ms=90_000.0,
            mean_ms=90.0,
            max_ms=900.0,
            rows=1000,
            query="select * from holdings where workspace_id = $1 " + "x" * 400,
        ),
        StatementRow(
            calls=2, total_ms=10_000.0, mean_ms=5000.0, max_ms=7000.0, rows=2, query="select 1"
        ),
    ]

    report = format_report(rows, limit=10)

    assert report.index("holdings") < report.index("select 1")
    assert "90.0%" in report and "10.0%" in report  # share of the listed total time
    assert "x" * 200 not in report  # truncated
    assert "$1" in report  # normalized text only: pg_stat_statements never stores bind values


def test_report_explains_how_to_enable_when_the_extension_is_missing():
    report = format_report(None, limit=10)
    assert "pg_stat_statements" in report and "shared_preload_libraries" in report


# ── 7. compose: log rotation + pg_stat_statements preload ───────────────────


def _load_compose(name: str) -> dict:
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)  # !override / !reset
    return yaml.load((REPO / name).read_text(), Loader=Loader)  # noqa: S506 - SafeLoader subclass


def test_every_prod_service_rotates_its_json_logs():
    services = _load_compose("docker-compose.prod.yml")["services"]
    assert {"api", "migrate", "cloudflared", "database-backup", "postgres", "redis"} <= set(
        services
    )
    for name, svc in services.items():
        logging_cfg = svc.get("logging") or {}
        options = logging_cfg.get("options") or {}
        assert logging_cfg.get("driver") == "json-file", name
        assert options.get("max-size") and options.get("max-file"), name


def test_postgres_preloads_pg_stat_statements():
    command = _load_compose("docker-compose.yml")["services"]["postgres"]["command"]
    assert "shared_preload_libraries=pg_stat_statements" in command


def test_prod_postgres_override_keeps_the_pg_stat_statements_preload():
    """Compose replaces (not merges) a service's `command`, so the prod override must repeat
    the preload flags or production silently loses pg_stat_statements on the next recreate."""
    command = _load_compose("docker-compose.prod.yml")["services"]["postgres"]["command"]
    assert command[0] == "postgres"
    assert "shared_preload_libraries=pg_stat_statements" in command
    assert "pg_stat_statements.track=top" in command
