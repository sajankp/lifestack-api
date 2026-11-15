"""spec-109: voice turn visibility, turn telemetry, VAD controls, model handling."""

import asyncio
import json

import pytest

from app.capture import agent as agent_module
from app.capture.agent import _handle_gemini_message, _modality_attempts
from app.capture.gemini_setup import _build_setup_message
from app.config import settings


class FakeClientWebSocket:
    def __init__(self):
        self.sent_json: list[dict] = []
        self.sent_bytes: list[bytes] = []

    async def send_json(self, payload: dict):
        self.sent_json.append(payload)

    async def send_bytes(self, payload: bytes):
        self.sent_bytes.append(payload)


class FakeGeminiWebSocket:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, payload: str):
        self.sent.append(payload)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


async def _read_log(path, kind, timeout=1.0):
    for _ in range(int(timeout / 0.02)):
        if path.exists():
            rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            hits = [r for r in rows if r["kind"] == kind]
            if hits:
                return hits
        await asyncio.sleep(0.02)
    return []


# ── VAD settings ────────────────────────────────────────────────────────────


def test_setup_has_no_realtime_input_config_by_default(monkeypatch):
    for name in (
        "CAPTURE_VAD_SILENCE_MS",
        "CAPTURE_VAD_PREFIX_PADDING_MS",
        "CAPTURE_VAD_START_SENSITIVITY",
        "CAPTURE_VAD_END_SENSITIVITY",
    ):
        monkeypatch.setattr(settings, name, None)

    assert "realtimeInputConfig" not in _build_setup_message(["AUDIO"])["setup"]


def test_setup_emits_configured_vad_fields_only(monkeypatch):
    monkeypatch.setattr(settings, "CAPTURE_VAD_SILENCE_MS", 700)
    monkeypatch.setattr(settings, "CAPTURE_VAD_PREFIX_PADDING_MS", None)
    monkeypatch.setattr(settings, "CAPTURE_VAD_START_SENSITIVITY", None)
    monkeypatch.setattr(settings, "CAPTURE_VAD_END_SENSITIVITY", "HIGH")

    aad = _build_setup_message(["AUDIO"])["setup"]["realtimeInputConfig"][
        "automaticActivityDetection"
    ]

    assert aad == {"silenceDurationMs": 700, "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH"}


# ── model handling ──────────────────────────────────────────────────────────


def test_modality_attempts_skip_text_probe_on_3x_models():
    assert _modality_attempts("models/gemini-3.1-flash-live-preview")[0] == ["AUDIO"]
    assert _modality_attempts("models/gemini-3.8-live")[0] == ["AUDIO"]
    assert _modality_attempts("models/gemini-2.5-flash-preview-native-audio-18-12")[0] == [
        "TEXT",
        "AUDIO",
    ]


def test_modality_attempts_keep_fallbacks():
    assert ["TEXT", "AUDIO"] in _modality_attempts("models/gemini-3.1-flash-live-preview")


def test_38_setup_omits_thinking_config_and_declares_tools_blocking(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MODEL", "models/gemini-3.8-live")

    setup = _build_setup_message(["AUDIO"])["setup"]

    assert "thinkingConfig" not in setup["generationConfig"]
    decls = setup["tools"][0]["functionDeclarations"]
    assert decls and all(d.get("behavior") == "BLOCKING" for d in decls)


def test_31_setup_keeps_thinking_budget_and_no_behavior(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MODEL", "models/gemini-3.1-flash-live-preview")

    setup = _build_setup_message(["AUDIO"])["setup"]

    assert "thinkingConfig" in setup["generationConfig"]
    assert all("behavior" not in d for d in setup["tools"][0]["functionDeclarations"])


# ── server → client turn messages ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_input_transcript_forwarded_to_client_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "CAPTURE_ENABLE_INPUT_TRANSCRIPTION", True)
    client_ws = FakeClientWebSocket()

    await _handle_gemini_message(
        {"serverContent": {"inputTranscription": {"text": "spent 250"}}},
        client_ws,  # type: ignore[arg-type]
        gemini_ws=None,
        user_id=1,
        workspace_id=1,
    )

    assert {"type": "input_transcript", "content": "spent 250"} in client_ws.sent_json


@pytest.mark.asyncio
async def test_input_transcript_not_forwarded_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "CAPTURE_ENABLE_INPUT_TRANSCRIPTION", False)
    client_ws = FakeClientWebSocket()

    await _handle_gemini_message(
        {"serverContent": {"inputTranscription": {"text": "spent 250"}}},
        client_ws,  # type: ignore[arg-type]
        gemini_ws=None,
        user_id=1,
        workspace_id=1,
    )

    assert client_ws.sent_json == []


@pytest.mark.asyncio
async def test_turn_complete_forwarded_to_client():
    client_ws = FakeClientWebSocket()

    await _handle_gemini_message(
        {"serverContent": {"turnComplete": True}},
        client_ws,  # type: ignore[arg-type]
        gemini_ws=None,
        user_id=1,
        workspace_id=1,
    )

    assert {"type": "turn_complete"} in client_ws.sent_json


# ── turn_timing capture-log row ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_timing_row_for_a_voice_turn_with_a_tool_call(tmp_path, monkeypatch):
    log_path = tmp_path / "turns.jsonl"
    monkeypatch.setattr(settings, "CAPTURE_TURN_LOG_PATH", str(log_path))
    monkeypatch.setattr(settings, "CAPTURE_ENABLE_INPUT_TRANSCRIPTION", True)
    clock = Clock()
    monkeypatch.setattr(agent_module, "_now", clock)

    async def fake_tool(name, args, user_id, workspace_id, tz):
        clock.t += 0.4  # tool runs 400 ms
        return {"status": "success"}

    monkeypatch.setattr(agent_module, "execute_agent_tool", fake_tool)
    client_ws = FakeClientWebSocket()
    gemini_ws = FakeGeminiWebSocket()
    turn_state: dict = {"assistant_text": [], "started_at": None}

    async def feed(msg):
        await _handle_gemini_message(
            msg,
            client_ws,  # type: ignore[arg-type]
            gemini_ws,
            user_id=7,
            workspace_id=8,
            session_id="sess-t",
            turn_state=turn_state,
        )

    await feed({"serverContent": {"inputTranscription": {"text": "spent "}}})  # t=100.0
    clock.t = 101.0
    await feed({"serverContent": {"inputTranscription": {"text": "250"}}})  # last input 101.0
    clock.t = 102.5  # model took 1.5 s to react
    await feed({"toolCall": {"functionCalls": [{"id": "c1", "name": "list_todos", "args": {}}]}})
    # tool took 0.4 -> clock 102.9
    clock.t = 103.0
    await feed({"serverContent": {"outputTranscription": {"text": "Logged."}}})
    clock.t = 104.0
    await feed({"serverContent": {"turnComplete": True}})

    rows = await _read_log(log_path, "turn_timing")
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "sess-t"
    assert row["input_ms"] == 1000
    assert row["input_end_to_first_tool_ms"] == 1500
    assert row["input_end_to_first_response_ms"] == 1500  # tool call is the first reaction
    assert row["tool_ms"] == 400
    assert row["tool_count"] == 1
    assert row["turn_ms"] == 3000
    assert row["interrupted"] is False
    assert row["has_input_transcript"] is True
    # no utterance text on the timing row
    assert "text" not in row


@pytest.mark.asyncio
async def test_turn_timing_resets_between_turns_and_flags_interrupts(tmp_path, monkeypatch):
    log_path = tmp_path / "turns.jsonl"
    monkeypatch.setattr(settings, "CAPTURE_TURN_LOG_PATH", str(log_path))
    monkeypatch.setattr(settings, "CAPTURE_ENABLE_INPUT_TRANSCRIPTION", False)
    clock = Clock()
    monkeypatch.setattr(agent_module, "_now", clock)
    client_ws = FakeClientWebSocket()
    turn_state: dict = {"assistant_text": [], "started_at": None}

    async def feed(msg):
        await _handle_gemini_message(
            msg,
            client_ws,  # type: ignore[arg-type]
            None,
            user_id=7,
            workspace_id=8,
            session_id="sess-i",
            turn_state=turn_state,
        )

    # typed turn: no input transcript, model audio then interrupted
    await feed({"serverContent": {"modelTurn": {"parts": [{"text": "hi"}]}}})
    await feed({"serverContent": {"interrupted": True}})
    clock.t = 101.0
    await feed({"serverContent": {"turnComplete": True}})
    # second turn must start clean
    clock.t = 110.0
    await feed({"serverContent": {"turnComplete": True}})

    rows = await _read_log(log_path, "turn_timing")
    for _ in range(25):
        rows = await _read_log(log_path, "turn_timing")
        if len(rows) >= 2:
            break
        await asyncio.sleep(0.02)
    assert len(rows) == 2
    assert rows[0]["interrupted"] is True
    assert rows[0]["has_input_transcript"] is False
    assert rows[0]["input_ms"] is None
    assert rows[0]["input_end_to_first_response_ms"] is None
    assert rows[1]["interrupted"] is False


@pytest.mark.asyncio
async def test_go_away_is_written_to_capture_log(tmp_path, monkeypatch):
    log_path = tmp_path / "turns.jsonl"
    monkeypatch.setattr(settings, "CAPTURE_TURN_LOG_PATH", str(log_path))

    await _handle_gemini_message(
        {"goAway": {"timeLeft": "30s"}},
        FakeClientWebSocket(),  # type: ignore[arg-type]
        None,
        user_id=7,
        workspace_id=8,
        session_id="sess-g",
    )

    rows = await _read_log(log_path, "go_away")
    assert rows and rows[0]["time_left"] == "30s" and rows[0]["session_id"] == "sess-g"


# ── audio_stream_end ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audio_stream_end_sends_flag_to_gemini_and_logs(tmp_path, monkeypatch):
    log_path = tmp_path / "turns.jsonl"
    monkeypatch.setattr(settings, "CAPTURE_TURN_LOG_PATH", str(log_path))
    gemini_ws = FakeGeminiWebSocket()

    await agent_module._handle_audio_stream_end(
        gemini_ws, user_id=7, workspace_id=8, session_id="sess-m", delay=0
    )

    assert json.loads(gemini_ws.sent[0]) == {"realtimeInput": {"audioStreamEnd": True}}
    rows = await _read_log(log_path, "mic_stopped")
    assert rows and rows[0]["session_id"] == "sess-m"


# ── session rows ────────────────────────────────────────────────────────────


def test_session_ended_row_is_written_to_capture_log(tmp_path, monkeypatch):
    log_path = tmp_path / "turns.jsonl"
    monkeypatch.setattr(settings, "CAPTURE_TURN_LOG_PATH", str(log_path))

    agent_module._log_session_ended(
        "client_disconnect", 12.34, user_id=7, workspace_id=8, session_id="sess-e"
    )

    row = json.loads(log_path.read_text().strip())
    assert row["kind"] == "session_ended"
    assert row["reason"] == "client_disconnect"
    assert row["duration_seconds"] == 12.3
    assert row["session_id"] == "sess-e"
