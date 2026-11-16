"""Per-tool execution time on the capture log's tool_call rows (follow-up to spec-109)."""

import asyncio
import json

import pytest

from app.capture import agent as agent_module
from app.capture.agent import _handle_gemini_message, _log_capture_turn
from app.capture.tool_dedup import CaptureToolDedupLedger, SessionDedupContext
from app.config import settings


class FakeClientWebSocket:
    def __init__(self):
        self.sent_json: list[dict] = []

    async def send_json(self, payload: dict):
        self.sent_json.append(payload)

    async def send_bytes(self, payload: bytes):  # pragma: no cover - unused
        pass


class FakeGeminiWebSocket:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, payload: str):
        self.sent.append(payload)


async def _tool_rows(path):
    for _ in range(50):
        if path.exists():
            rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            hits = [r for r in rows if r["kind"] == "tool_call"]
            if hits:
                return hits
        await asyncio.sleep(0.02)
    return []


def test_tool_call_row_carries_duration_ms(tmp_path, monkeypatch):
    log_path = tmp_path / "turns.jsonl"
    monkeypatch.setattr(settings, "CAPTURE_TURN_LOG_PATH", str(log_path))

    _log_capture_turn(
        "log_weight", {"weight_kg": "72"}, "success", user_id=1, workspace_id=1, duration_ms=412.3
    )

    assert json.loads(log_path.read_text())["duration_ms"] == 412.3


def test_tool_call_row_duration_defaults_to_null(tmp_path, monkeypatch):
    log_path = tmp_path / "turns.jsonl"
    monkeypatch.setattr(settings, "CAPTURE_TURN_LOG_PATH", str(log_path))

    _log_capture_turn("log_weight", {"weight_kg": "72"}, "success", user_id=1, workspace_id=1)

    assert json.loads(log_path.read_text())["duration_ms"] is None


@pytest.mark.asyncio
async def test_executed_tool_is_timed_end_to_end(tmp_path, monkeypatch):
    log_path = tmp_path / "turns.jsonl"
    monkeypatch.setattr(settings, "CAPTURE_TURN_LOG_PATH", str(log_path))
    clock = {"t": 100.0}
    monkeypatch.setattr(agent_module, "_now", lambda: clock["t"])

    async def slow_tool(name, args, user_id, workspace_id, tz):
        clock["t"] += 2.5
        return {"status": "success"}

    monkeypatch.setattr(agent_module, "execute_agent_tool", slow_tool)

    await _handle_gemini_message(
        {"toolCall": {"functionCalls": [{"id": "c1", "name": "list_todos", "args": {}}]}},
        FakeClientWebSocket(),  # type: ignore[arg-type]
        FakeGeminiWebSocket(),
        user_id=7,
        workspace_id=8,
        session_id="sess-d",
    )

    rows = await _tool_rows(log_path)
    assert rows[0]["tool"] == "list_todos"
    assert rows[0]["duration_ms"] == 2500.0


@pytest.mark.asyncio
async def test_suppressed_replay_logs_null_duration(tmp_path, monkeypatch):
    log_path = tmp_path / "turns.jsonl"
    monkeypatch.setattr(settings, "CAPTURE_TURN_LOG_PATH", str(log_path))
    ledger = CaptureToolDedupLedger(window_seconds=2700)
    args = {"amount": "40", "category_name": "food", "description": "coffee"}
    ledger.record(
        workspace_id=8,
        user_id=7,
        tool="log_spending_transaction",
        args=args,
        result={"status": "success", "entity_public_id": "txn-orig"},
        user_timezone="UTC",
    )

    async def _must_not_execute(*a, **k):
        raise AssertionError("must not execute a suppressed replay")

    monkeypatch.setattr(agent_module, "execute_agent_tool", _must_not_execute)

    await _handle_gemini_message(
        {
            "toolCall": {
                "functionCalls": [{"id": "c2", "name": "log_spending_transaction", "args": args}]
            }
        },
        FakeClientWebSocket(),  # type: ignore[arg-type]
        FakeGeminiWebSocket(),
        user_id=7,
        workspace_id=8,
        session_id="sess-r",
        dedup_ctx=SessionDedupContext(ledger=ledger, resumed=True),
    )

    rows = await _tool_rows(log_path)
    assert rows[0]["status"] == "duplicate_suppressed"
    assert rows[0]["duration_ms"] is None
