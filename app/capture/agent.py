import asyncio
import base64
import inspect
import json
from contextlib import suppress

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
                "get_investing_summary": tools.get_investing_summary,
                "list_todos": tools.list_todos,
                "get_todo": tools.get_todo,
                "update_todo": tools.update_todo,
                "delete_todo": tools.delete_todo,
                "list_next_due_items": tools.list_next_due_items,
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
    gemini_url: str, user_timezone: str = "UTC", workspace_context: str = ""
) -> tuple:
    """
    Connect to the Gemini Live API using GEMINI_MODEL.
    Returns (websocket, context_manager) on success.
    Raises RuntimeError if connection or setup fails.
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


async def _handle_gemini_message(
    msg: dict,
    client_ws: WebSocket,
    gemini_ws,
    user_id: int,
    workspace_id: int,
    user_timezone: str = "UTC",
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
                    await client_ws.send_json({"type": "transcript", "content": text})

                # Audio blob — raw 24kHz 16-bit PCM, base64-encoded
                inline_data = part.get("inlineData")
                if inline_data:
                    audio_b64 = inline_data.get("data")
                    if audio_b64:
                        audio_bytes = base64.b64decode(audio_b64)
                        await client_ws.send_bytes(audio_bytes)

    # ── toolCall ─────────────────────────────────────────────────────────────
    tool_call = msg.get("toolCall")
    if tool_call:
        function_calls = tool_call.get("functionCalls") or []
        for fc in function_calls:
            call_id = fc.get("id")
            name = fc.get("name")
            args = fc.get("args") or {}

            await client_ws.send_json({"type": "tool_call", "name": name, "arguments": args})

            result = await execute_agent_tool(
                name,
                args,
                user_id,
                workspace_id,
                user_timezone,
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
):
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

    # Fetch the workspace's category/account vocabulary once, before the session
    # opens (spec-055). A failure here must not sink the whole session — fall
    # back to an empty context (the prompt still works, just without the list).
    try:
        workspace_context = await _fetch_workspace_context(workspace_id)
    except Exception as exc:
        logger.warning("capture_workspace_context_fetch_failed", error=str(exc))
        workspace_context = ""

    try:
        gemini_ws, ws_context_manager = await _connect_gemini(
            gemini_url, user_timezone, workspace_context
        )
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
                    await gemini_ws.send(
                        json.dumps({
                            "realtimeInput": {
                                "mediaChunks": [
                                    {"mimeType": "audio/pcm;rate=16000", "data": b64_data}
                                ]
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
                    # Encoded audio (WebM/Opus etc.) — ffmpeg decodes to PCM
                    await decoder.send_encoded_chunk(message["bytes"])

                elif message.get("text") is not None:
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
                    logger.info("client_websocket_disconnected")
                elif exc:
                    raise exc
        finally:
            for task in [pcm_task, gemini_task, client_task]:
                task.cancel()
            await asyncio.gather(pcm_task, gemini_task, client_task, return_exceptions=True)

    except Exception as e:
        logger.error("gemini_live_session_error", error=str(e))
        await _send_capture_error(client_ws, CAPTURE_CLIENT_ERROR)
    finally:
        await decoder.close()
        if ws_context_manager is not None:
            with suppress(Exception):
                await ws_context_manager.__aexit__(None, None, None)
