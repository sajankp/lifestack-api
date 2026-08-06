import asyncio
import base64
import inspect
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import structlog
import websockets
from fastapi import WebSocket, WebSocketDisconnect

from app.capture.audio import AudioDecoder
from app.capture.gemini_setup import _build_setup_message, _fetch_workspace_context
from app.capture.session_limiter import (
    CAPTURE_POLICY_VIOLATION_CLOSE_CODE,
    CaptureSessionLimiter,
    CaptureSessionLimitExceededError,
)
from app.capture.tool_dedup import (
    WRITE_TOOLS,
    SessionDedupContext,
    get_global_ledger,
)
from app.capture.tools import AgentTools
from app.config import settings
from app.core.database import postgres

logger = structlog.get_logger(__name__)

# GEMINI live endpoint and model are configured via `settings.GEMINI_LIVE_URL`
# and `settings.GEMINI_MODEL` (env-configurable).

CAPTURE_PROVIDER_UNAVAILABLE_CLOSE_CODE = 4002
CAPTURE_CLIENT_ERROR = "Voice capture is temporarily unavailable. Please try again."
CAPTURE_PROVIDER_ERROR = "Voice provider returned an error. Please try again."
CAPTURE_INVALID_MESSAGE_ERROR = "Voice capture received an invalid client message."


# Single-worker executor so offloaded log writes append in submission order —
# with the default (multi-threaded) executor, a turn's user_transcript and
# assistant_transcript rows could land in the file out of order.
_capture_log_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture-log")


def _log_capture_event(entry: dict) -> None:
    """Append one JSONL entry to the capture log (spec-079 Stage A/B).

    Feature-off (no writes) unless `settings.CAPTURE_TURN_LOG_PATH` is set;
    production points it at a bind-mounted host path (see docker-compose.yml) so
    it survives container recreation, unlike the stdout-only structured logs. A
    write failure must never sink the voice session, so all I/O errors are
    swallowed. The write itself is blocking disk I/O (mkdir/open/write); run
    inline in the live agent session it would block the event loop that's also
    driving the audio pipeline, so it's offloaded to a worker thread via
    `run_in_executor` whenever a loop is running, falling back to inline
    execution for sync callers (e.g. unit tests with no running loop).
    """
    path = settings.CAPTURE_TURN_LOG_PATH
    if not path:
        return
    entry = {"timestamp": datetime.now(UTC).isoformat(), **entry}

    def _write() -> None:
        try:
            log_path = Path(path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as exc:
            logger.warning("capture_turn_log_write_failed", error=str(exc))

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _write()
    else:
        try:
            loop.run_in_executor(_capture_log_executor, _write)
        except RuntimeError:
            # e.g. the loop is closing (app shutdown) — a write failure must
            # never sink the caller; fall back to a synchronous write.
            _write()


def _log_capture_turn(
    tool_name: str,
    args: dict,
    status: str,
    *,
    user_id: int,
    workspace_id: int,
    session_id: str | None = None,
    call_id: str | None = None,
) -> None:
    """Record one voice tool-call turn (`kind='tool_call'`): tool name, args, and
    outcome. The user's utterance is not on this row — with input transcription
    enabled it lands as a separate `kind='user_transcript'` event (spec-079 Q4,
    metered free). `session_id` groups a conversation's turns; see
    `_log_capture_event` for the write semantics.
    """
    _log_capture_event({
        "kind": "tool_call",
        "session_id": session_id,
        "call_id": call_id,
        "tool": tool_name,
        "args": args,
        "status": status,
        "user_id": user_id,
        "workspace_id": workspace_id,
    })


def _log_assistant_transcript(
    text: str,
    *,
    user_id: int,
    workspace_id: int,
    session_id: str | None = None,
    generation_ms: float | None = None,
) -> None:
    """Record the assistant's spoken reply for a turn (`kind='assistant_transcript'`),
    captured from Gemini output transcription (spec-079 Stage B, metered free).
    `generation_ms` is the turn's generation span (first response chunk →
    turnComplete) — the metered latency driver is reply length, so a long span
    flags a long-winded turn. No-op on empty text.
    """
    if not text.strip():
        return
    _log_capture_event({
        "kind": "assistant_transcript",
        "session_id": session_id,
        "text": text,
        "generation_ms": round(generation_ms, 1) if generation_ms is not None else None,
        "user_id": user_id,
        "workspace_id": workspace_id,
    })


def _log_user_transcript(
    text: str,
    *,
    user_id: int,
    workspace_id: int,
    session_id: str | None = None,
) -> None:
    """Record the user's spoken utterance for a turn (`kind='user_transcript'`),
    captured from Gemini input transcription (spec-079 Q4, metered free). This is
    the real-usage utterance source for the eval set's 60% slice. No-op on empty
    text.
    """
    if not text.strip():
        return
    _log_capture_event({
        "kind": "user_transcript",
        "session_id": session_id,
        "text": text,
        "user_id": user_id,
        "workspace_id": workspace_id,
    })


def _log_session_ended(reason: str, duration_seconds: float) -> None:
    """Structured, PII-redacted disconnect/resume-failure instrumentation
    (spec-079 Stage A). Emits a count-only event — no transcript, audio, or
    user-authored content — so production logs can be aggregated into a
    disconnect-rate-by-reason metric ahead of Stage B transport work.
    """
    logger.info(
        "capture_session_ended",
        reason=reason,
        duration_seconds=round(duration_seconds, 1),
    )


async def _send_capture_error(
    client_ws: WebSocket,
    message: str,
    *,
    close_code: int | None = None,
) -> None:
    with suppress(Exception):
        await client_ws.send_json({"type": "error", "message": message})
    if close_code is not None:
        with suppress(Exception):
            await client_ws.close(code=close_code)


async def execute_agent_tool(
    name: str,
    args: dict,
    user_id: int,
    workspace_id: int,
    user_timezone: str = "UTC",
) -> dict:
    async with postgres.async_session_maker() as session:
        try:
            tools = AgentTools(
                session=session,
                user_id=user_id,
                workspace_id=workspace_id,
                user_timezone=user_timezone,
            )
            dispatch = {
                "create_todo_task": tools.create_todo_task,
                "create_recurring_todo": tools.create_recurring_todo,
                "log_spending_transaction": tools.log_spending_transaction,
                "list_spending_transactions": tools.list_spending_transactions,
                "get_investing_summary": tools.get_investing_summary,
                "get_account_balances": tools.get_account_balances,
                "list_todos": tools.list_todos,
                "get_todo": tools.get_todo,
                "update_todo": tools.update_todo,
                "delete_todo": tools.delete_todo,
                "list_next_due_items": tools.list_next_due_items,
                "log_weight": tools.log_weight,
                "log_medication_event": tools.log_medication_event,
            }

            if name in dispatch:
                fn = dispatch[name]
                sig = inspect.signature(fn)
                call_kwargs = {}
                for p in sig.parameters.values():
                    if p.name in args:
                        call_kwargs[p.name] = args[p.name]
                res = await fn(**call_kwargs)
            else:
                res = {"status": "error", "message": f"Unknown function: {name}"}

            await session.commit()
            return res
        except Exception as e:
            await session.rollback()
            logger.error("tool_execution_failed", tool=name, error=str(e))
            return {
                "status": "error",
                "message": "An internal error occurred while executing the tool.",
            }


# Caps keep the injected context small and bounded (≤70 short names once per
# session) — see spec-055 §1.
async def _connect_gemini(
    gemini_url: str,
    user_timezone: str = "UTC",
    workspace_context: str = "",
    resumption_handle: str | None = None,
) -> tuple:
    """
    Connect to the Gemini Live API using GEMINI_MODEL.
    Returns (websocket, context_manager) on success.
    Raises RuntimeError if connection or setup fails.

    `resumption_handle` (spec-079 Stage B) resumes a prior session's conversation
    context when session resumption is enabled; ignored when the flag is off.
    """
    logger.info("connecting_to_gemini", model=settings.GEMINI_MODEL)

    # Try a sequence of response modality options to handle provider/model changes
    modality_attempts = [
        ["TEXT", "AUDIO"],
        ["TEXT"],
        ["AUDIO"],
    ]

    last_err: Exception | None = None
    for modalities in modality_attempts:
        ws_conn = websockets.connect(gemini_url)
        ws = await ws_conn.__aenter__()
        try:
            setup_message = _build_setup_message(
                response_modalities=modalities,
                user_timezone=user_timezone,
                workspace_context=workspace_context,
                resumption_handle=resumption_handle,
            )
            await ws.send(json.dumps(setup_message))

            first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=8.0)
            first_msg = json.loads(first_msg_raw)

            # Successful setup returns a setupComplete envelope
            if "setupComplete" in first_msg:
                logger.info(
                    "gemini_ws_setup_completed",
                    model=settings.GEMINI_MODEL,
                    modalities=modalities,
                )
                return ws, ws_conn

            # If the response explicitly rejects the response modalities, try next set
            err_text = json.dumps(first_msg)
            if "response modalities" in err_text or "requested combination" in err_text:
                logger.warning(
                    "gemini_modality_rejected",
                    model=settings.GEMINI_MODEL,
                    tried=modalities,
                    response=first_msg,
                )
                with suppress(Exception):
                    await ws_conn.__aexit__(None, None, None)
                continue

            # Unexpected response — raise and abort
            raise RuntimeError(f"Unexpected setup response from Gemini: {first_msg}")

        except Exception as exc:
            last_err = exc
            with suppress(Exception):
                await ws_conn.__aexit__(None, None, None)
            # If the websocket closed with a 1007 close code and includes the
            # modality-rejection text, treat as modalilty rejection and retry.
            try:
                # Some websocket implementations expose a `close_code` attribute
                # on the exception or on the underlying close event. Check common
                # places safely.
                close_code = getattr(exc, "code", None) or getattr(exc, "close_code", None)
                close_reason = getattr(exc, "reason", None) or str(exc)
                if close_code == 1007 and (
                    "response modalities" in str(close_reason)
                    or "requested combination" in str(close_reason)
                ):
                    continue
            except Exception:
                pass
            # Fall back to checking the message text if structured attributes are unavailable.
            if "response modalities" in str(exc) or "requested combination" in str(exc):
                continue
            # For other errors (e.g. connection, timeout, auth), fail fast instead of retrying
            break

    # Exhausted attempts
    if last_err is not None:
        raise last_err
    raise RuntimeError("Failed to establish Gemini WebSocket connection")


def _accumulate_assistant_text(turn_state: dict | None, text: str) -> None:
    """Buffer an assistant reply fragment for the current turn (spec-079 Stage B).
    Records the first-fragment time so the turn's generation span can be logged.
    No-op when there is no turn_state (callers that don't want turn logging)."""
    if turn_state is None:
        return
    if turn_state.get("started_at") is None:
        turn_state["started_at"] = time.monotonic()
    turn_state.setdefault("assistant_text", []).append(text)


def _accumulate_user_text(turn_state: dict | None, text: str) -> None:
    """Buffer a user-utterance fragment from input transcription for the current
    turn (spec-079 Q4). No-op when there is no turn_state."""
    if turn_state is None:
        return
    turn_state.setdefault("user_text", []).append(text)


def _flush_user_turn(
    turn_state: dict, *, user_id: int, workspace_id: int, session_id: str | None
) -> None:
    """Write the accumulated user utterance for a completed turn to the capture
    log, then reset the buffer for the next turn (spec-079 Q4)."""
    fragments = turn_state.get("user_text") or []
    turn_state["user_text"] = []
    if not fragments:
        return
    _log_user_transcript(
        "".join(fragments),
        user_id=user_id,
        workspace_id=workspace_id,
        session_id=session_id,
    )


def _flush_assistant_turn(
    turn_state: dict, *, user_id: int, workspace_id: int, session_id: str | None
) -> None:
    """Write the accumulated assistant reply for a completed turn to the capture
    log, then reset the buffer for the next turn (spec-079 Stage B)."""
    fragments = turn_state.get("assistant_text") or []
    started_at = turn_state.get("started_at")
    turn_state["assistant_text"] = []
    turn_state["started_at"] = None
    if not fragments:
        return
    generation_ms = (time.monotonic() - started_at) * 1000 if started_at is not None else None
    _log_assistant_transcript(
        "".join(fragments),
        user_id=user_id,
        workspace_id=workspace_id,
        session_id=session_id,
        generation_ms=generation_ms,
    )


async def _handle_gemini_message(
    msg: dict,
    client_ws: WebSocket,
    gemini_ws,
    user_id: int,
    workspace_id: int,
    user_timezone: str = "UTC",
    *,
    session_id: str | None = None,
    turn_state: dict | None = None,
    dedup_ctx: SessionDedupContext | None = None,
):
    """
    Parse a single message from Gemini and forward content to the client.

    Gemini 3.1 Flash Live difference from 2.5:
    A single serverContent event may contain MULTIPLE parts simultaneously
    (e.g., inlineData audio blob AND a transcript text part in the same event).
    We must iterate ALL parts in every event — not assume one-part-per-event.
    """
    # ── error (server-side model/API errors) ─────────────────────────────────
    gemini_error = msg.get("error")
    if gemini_error:
        error_msg = gemini_error.get("message", "Unknown error from Gemini API")
        logger.error("gemini_api_error", error=error_msg)
        await _send_capture_error(client_ws, CAPTURE_PROVIDER_ERROR)
        return
    # ── sessionResumptionUpdate (spec-079 Stage B) ───────────────────────────
    # Gemini periodically emits a resumption handle for the live session. Forward
    # the latest usable handle to the client so that, if its WebSocket drops, it
    # can reconnect with `?resume=<handle>` and Gemini restores the conversation
    # context. A `resumable: false` update carries no handle worth keeping.
    resumption_update = msg.get("sessionResumptionUpdate")
    if resumption_update:
        new_handle = resumption_update.get("newHandle")
        if new_handle and resumption_update.get("resumable") is not False:
            await client_ws.send_json({"type": "session_resumption", "handle": new_handle})
        return
    # ── goAway (spec-079 Stage B) ────────────────────────────────────────────
    # Gemini warns of an imminent server-side disconnect. Surface it as a session
    # state so the client can reconnect proactively before the hard close.
    go_away = msg.get("goAway")
    if go_away:
        time_left = go_away.get("timeLeft")
        logger.info("gemini_go_away", time_left=str(time_left))
        await client_ws.send_json({
            "type": "session_state",
            "state": "closing",
            "time_left": time_left,
        })
        return
    # ── serverContent ────────────────────────────────────────────────────────
    server_content = msg.get("serverContent")
    if server_content:
        # Barge-in (spec-059): Gemini's VAD heard the user talk over the model
        # and stopped generating. The client schedules audio ahead of real
        # time, so it must be told to flush its queue or buffered speech keeps
        # playing to the end.
        if server_content.get("interrupted"):
            await client_ws.send_json({"type": "interrupted"})

        model_turn = server_content.get("modelTurn")
        if model_turn:
            parts = model_turn.get("parts") or []
            for part in parts:
                # Transcript text (may arrive in same event as audio on 3.1)
                text = part.get("text")
                if text:
                    _accumulate_assistant_text(turn_state, text)
                    await client_ws.send_json({"type": "transcript", "content": text})

                # Audio blob — raw 24kHz 16-bit PCM, base64-encoded
                inline_data = part.get("inlineData")
                if inline_data:
                    audio_b64 = inline_data.get("data")
                    if audio_b64:
                        audio_bytes = base64.b64decode(audio_b64)
                        await client_ws.send_bytes(audio_bytes)

        # Output transcription (spec-079 Stage B): native-audio models emit the
        # assistant's spoken reply here as text, separate from modelTurn parts.
        # Forward as the same caption channel and accumulate for the turn log.
        output_transcription = server_content.get("outputTranscription")
        if output_transcription and (ot_text := output_transcription.get("text")):
            _accumulate_assistant_text(turn_state, ot_text)
            await client_ws.send_json({"type": "transcript", "content": ot_text})

        # Input transcription (spec-079 Q4): the user's own words as heard by the
        # model. Log-only — never echoed back on the assistant caption channel.
        # Gated on the flag here as well as in the setup message: 3.1 Flash Live
        # emits inputTranscription even when not requested, and flag-off must
        # mean utterance text is never persisted.
        if settings.CAPTURE_ENABLE_INPUT_TRANSCRIPTION:
            input_transcription = server_content.get("inputTranscription")
            if input_transcription and (it_text := input_transcription.get("text")):
                _accumulate_user_text(turn_state, it_text)

        # Turn boundary: flush the accumulated user utterance then the assistant
        # reply (conversational order) to the capture log, resetting both buffers.
        if server_content.get("turnComplete") and turn_state is not None:
            _flush_user_turn(
                turn_state, user_id=user_id, workspace_id=workspace_id, session_id=session_id
            )
            _flush_assistant_turn(
                turn_state, user_id=user_id, workspace_id=workspace_id, session_id=session_id
            )

    # ── toolCall ─────────────────────────────────────────────────────────────
    tool_call = msg.get("toolCall")
    if tool_call:
        function_calls = tool_call.get("functionCalls") or []
        for fc in function_calls:
            call_id = fc.get("id")
            name = fc.get("name")
            args = fc.get("args") or {}

            await client_ws.send_json({"type": "tool_call", "name": name, "arguments": args})

            # spec-090 replay guard: on a resumed connection where the user has
            # not yet spoken/typed, a write call matching a recent successful
            # execution is Gemini re-emitting a call whose side effect already
            # committed (toolResponse lost in the drop) — return the original
            # result instead of double-executing. Measured at ≥15% of write
            # executions before the guard; see the spec's 2026-07-22 addendum.
            result = None
            if dedup_ctx is not None and name in WRITE_TOOLS and dedup_ctx.replay_suspect:
                original = dedup_ctx.ledger.check_replay(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    tool=name,
                    args=args,
                    user_timezone=user_timezone,
                )
                if original is not None:
                    result = {**original, "status": "duplicate_suppressed"}
                    logger.info(
                        "capture_tool_replay_suppressed",
                        tool=name,
                        user_id=user_id,
                        workspace_id=workspace_id,
                    )

            if result is None:
                result = await execute_agent_tool(
                    name,
                    args,
                    user_id,
                    workspace_id,
                    user_timezone,
                )
                if (
                    dedup_ctx is not None
                    and name in WRITE_TOOLS
                    and result.get("status") == "success"
                ):
                    dedup_ctx.ledger.record(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        tool=name,
                        args=args,
                        result=result,
                        user_timezone=user_timezone,
                    )
            _log_capture_turn(
                name,
                args,
                result.get("status", "success"),
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                call_id=call_id,
            )

            await client_ws.send_json({
                "type": "tool_response",
                "name": name,
                "status": result.get("status", "success"),
                "entity_id": result.get("entity_public_id"),
                "result": result,
            })

            # Return result to Gemini — sequential on 3.1 (NON_BLOCKING not supported)
            tool_response_payload = {
                "toolResponse": {
                    "functionResponses": [
                        {
                            "id": call_id,
                            "name": name,
                            "response": {"output": result},
                        }
                    ]
                }
            }
            await gemini_ws.send(json.dumps(tool_response_payload))


async def run_agent_session(
    client_ws: WebSocket,
    user_id: int,
    workspace_id: int,
    user_timezone: str = "UTC",
    resumption_handle: str | None = None,
    prev_session_id: str | None = None,
):
    # spec-090: one id per WS session (also used further down to group capture
    # log turns). Announced to the client first thing so it can send it back as
    # `?prev_session=` when it reconnects with a resumption handle — that field
    # is what lets the capture log correlate a resumed session with the
    # connection it resumed from.
    session_id = uuid.uuid4().hex
    with suppress(Exception):
        await client_ws.send_json({"type": "session_info", "session_id": session_id})
    # Replay guard state for this connection (spec-090): the ledger is process
    # wide (replays land on NEW connections); resumed-ness gates suppression.
    dedup_ctx = SessionDedupContext(
        ledger=get_global_ledger(), resumed=resumption_handle is not None
    )
    _log_capture_event({
        "kind": "session_started",
        "session_id": session_id,
        "resumed": resumption_handle is not None,
        "resume_of_session_id": prev_session_id,
        "user_id": user_id,
        "workspace_id": workspace_id,
    })

    api_key = settings.GEMINI_API_KEY
    if not api_key:
        logger.error("gemini_api_key_missing")
        await _send_capture_error(
            client_ws,
            CAPTURE_CLIENT_ERROR,
            close_code=CAPTURE_PROVIDER_UNAVAILABLE_CLOSE_CODE,
        )
        return

    gemini_url = f"{settings.GEMINI_LIVE_URL}?key={api_key}"
    decoder = AudioDecoder()
    await decoder.start()
    limiter = CaptureSessionLimiter.from_settings()

    gemini_ws = None
    ws_context_manager = None
    session_started_at = time.monotonic()
    # spec-079 Stage B: the mutable turn_state accumulates the assistant's reply
    # across messages so a full turn can be logged at its turnComplete boundary
    # (session_id itself is minted at the top of this function, spec-090).
    turn_state: dict = {"assistant_text": [], "started_at": None}
    # spec-079 Stage A: mutable holder so nested closures can record why the
    # session ended, for the disconnect/resume-failure instrumentation logged
    # once in the outer `finally` below.
    session_outcome = {"reason": "normal"}

    # Fetch the workspace's category/account vocabulary once, before the session
    # opens (spec-055). A failure here must not sink the whole session — fall
    # back to an empty context (the prompt still works, just without the list).
    try:
        workspace_context = await _fetch_workspace_context(workspace_id)
    except Exception as exc:
        logger.warning("capture_workspace_context_fetch_failed", error=str(exc))
        workspace_context = ""

    try:
        try:
            gemini_ws, ws_context_manager = await _connect_gemini(
                gemini_url, user_timezone, workspace_context, resumption_handle
            )
        except Exception:
            session_outcome["reason"] = "gemini_connect_failed"
            raise
        logger.info("gemini_session_active", model=settings.GEMINI_MODEL)

        # ── Background: stream decoded PCM → Gemini ───────────────────────────
        async def pcm_to_gemini_loop():
            try:
                while True:
                    # 2048 bytes of 16kHz 16-bit mono PCM ≈ 64ms of audio
                    chunk = await decoder.read_pcm_chunk(2048)
                    if not chunk:
                        break

                    b64_data = base64.b64encode(chunk).decode("utf-8")
                    # `realtimeInput.mediaChunks` is deprecated and gemini-3.1-flash-live-preview
                    # hard-rejects it (1007 close); `realtimeInput.audio` (singular object, not a
                    # list) is accepted by both that model and gemini-2.5-flash-native-audio
                    # (verified live, spec-079) — use the one schema for both.
                    await gemini_ws.send(
                        json.dumps({
                            "realtimeInput": {
                                "audio": {"mimeType": "audio/pcm;rate=16000", "data": b64_data}
                            }
                        })
                    )
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("pcm_to_gemini_loop_error", error=str(e))

        # ── Background: Gemini responses → Client ────────────────────────────
        async def gemini_to_client_loop():
            try:
                async for raw_msg in gemini_ws:
                    msg = json.loads(raw_msg)
                    # Log every Gemini message at debug level to trace empty-output issues.
                    # Keys only (no audio data) to keep logs readable.
                    logger.debug("gemini_raw_message", keys=list(msg.keys()))
                    await _handle_gemini_message(
                        msg,
                        client_ws,
                        gemini_ws,
                        user_id,
                        workspace_id,
                        user_timezone,
                        session_id=session_id,
                        turn_state=turn_state,
                        dedup_ctx=dedup_ctx,
                    )
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("gemini_to_client_loop_error", error=str(e))

        async def client_to_gemini_loop():
            while True:
                message = await client_ws.receive()
                try:
                    limiter.validate_client_message(message)
                except CaptureSessionLimitExceededError as exc:
                    logger.warning(
                        "capture_session_limit_exceeded",
                        detail=exc.detail,
                        user_id=user_id,
                        workspace_id=workspace_id,
                    )
                    await _send_capture_error(
                        client_ws,
                        exc.detail,
                        close_code=exc.close_code,
                    )
                    return

                if message.get("bytes") is not None:
                    # Real user activity on this connection — from here on,
                    # identical write calls are intentional, not replays
                    # (spec-090).
                    dedup_ctx.user_input_seen = True
                    # Encoded audio (WebM/Opus etc.) — ffmpeg decodes to PCM
                    await decoder.send_encoded_chunk(message["bytes"])

                elif message.get("text") is not None:
                    dedup_ctx.user_input_seen = True
                    try:
                        client_msg = json.loads(message["text"])
                    except json.JSONDecodeError:
                        logger.warning(
                            "capture_invalid_client_json",
                            user_id=user_id,
                            workspace_id=workspace_id,
                        )
                        await _send_capture_error(
                            client_ws,
                            CAPTURE_INVALID_MESSAGE_ERROR,
                            close_code=CAPTURE_POLICY_VIOLATION_CLOSE_CODE,
                        )
                        return
                    msg_type = client_msg.get("type")

                    if msg_type == "text":
                        content = client_msg.get("content", "")
                        # Gemini 3.1: use realtimeInput for live text (not clientContent)
                        await gemini_ws.send(json.dumps({"realtimeInput": {"text": content}}))

        pcm_task = asyncio.create_task(pcm_to_gemini_loop())
        gemini_task = asyncio.create_task(gemini_to_client_loop())
        client_task = asyncio.create_task(client_to_gemini_loop())
        try:
            done, _ = await asyncio.wait(
                [pcm_task, gemini_task, client_task],
                timeout=settings.CAPTURE_MAX_SESSION_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                session_outcome["reason"] = "session_duration_exceeded"
                logger.warning(
                    "capture_session_duration_exceeded",
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
                await _send_capture_error(
                    client_ws,
                    "Voice session time limit reached.",
                    close_code=CAPTURE_POLICY_VIOLATION_CLOSE_CODE,
                )
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc and isinstance(exc, WebSocketDisconnect):
                    session_outcome["reason"] = "client_disconnect"
                    logger.info("client_websocket_disconnected")
                elif exc:
                    session_outcome["reason"] = "gemini_stream_error"
                    raise exc
                elif task is client_task and session_outcome["reason"] == "normal":
                    # client_to_gemini_loop only returns without raising when
                    # the session limiter closed the connection.
                    session_outcome["reason"] = "policy_violation"
        finally:
            for task in [pcm_task, gemini_task, client_task]:
                task.cancel()
            await asyncio.gather(pcm_task, gemini_task, client_task, return_exceptions=True)

    except Exception as e:
        if session_outcome["reason"] == "normal":
            session_outcome["reason"] = "gemini_stream_error"
        logger.error("gemini_live_session_error", error=str(e))
        await _send_capture_error(client_ws, CAPTURE_CLIENT_ERROR)
    finally:
        await decoder.close()
        if ws_context_manager is not None:
            with suppress(Exception):
                await ws_context_manager.__aexit__(None, None, None)
        _log_session_ended(session_outcome["reason"], time.monotonic() - session_started_at)
