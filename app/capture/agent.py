import asyncio
import base64
import json
from contextlib import suppress

import structlog
import websockets
from fastapi import WebSocket, WebSocketDisconnect

from app.capture.tools import AgentTools
from app.config import settings
from app.core.database import postgres

logger = structlog.get_logger(__name__)

GEMINI_LIVE_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"

# Active model — Gemini 2.5 Flash Native Audio.
# This model supports ["TEXT", "AUDIO"] responseModalities which is required
# for function calling (the model emits text during tool-call reasoning).
#
# gemini-3.1-flash-live-preview is NOT used because it only supports ["AUDIO"]
# modality, which conflicts with function calling (empty-output error on tool turns).
# Uncomment when 3.1 gains function-calling + TEXT support:
#   GEMINI_MODEL = "models/gemini-3.1-flash-live-preview"
GEMINI_MODEL = "models/gemini-2.5-flash-native-audio-latest"


class AudioDecoder:
    def __init__(self):
        self.process = None

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def send_encoded_chunk(self, chunk: bytes):
        if self.process and self.process.stdin:
            try:
                self.process.stdin.write(chunk)
                await self.process.stdin.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass

    async def read_pcm_chunk(self, size: int = 1024) -> bytes:
        if self.process and self.process.stdout:
            try:
                return await self.process.stdout.read(size)
            except Exception:
                return b""
        return b""

    async def close(self):
        if self.process:
            try:
                if self.process.stdin:
                    self.process.stdin.close()
                    await self.process.stdin.wait_closed()
            except Exception:
                pass
            try:
                self.process.terminate()
                await self.process.wait()
            except Exception:
                pass


async def execute_agent_tool(name: str, args: dict, user_id: int, workspace_id: int) -> dict:
    async with postgres.async_session_maker() as session:
        try:
            tools = AgentTools(session=session, user_id=user_id, workspace_id=workspace_id)
            if name == "create_todo_task":
                res = await tools.create_todo_task(
                    title=args.get("title", ""),
                    due_date=args.get("due_date"),
                    priority=args.get("priority", "medium"),
                )
            elif name == "log_spending_transaction":
                res = await tools.log_spending_transaction(
                    amount=args.get("amount", "0"),
                    category_name=args.get("category_name", "other"),
                    description=args.get("description", ""),
                )
            elif name == "log_cash_balance":
                res = await tools.log_cash_balance(
                    account_name=args.get("account_name", ""),
                    balance=args.get("balance", "0"),
                    currency=args.get("currency", "USD"),
                )
            else:
                res = {"status": "error", "message": f"Unknown function: {name}"}

            await session.commit()
            return res
        except Exception as e:
            await session.rollback()
            logger.error("tool_execution_failed", tool=name, error=str(e))
            return {"status": "error", "message": str(e)}


def _build_setup_message() -> dict:
    """
    Build the Gemini Live API setup payload for Gemini 2.5 Flash Native Audio.

    Key settings:
    - responseModalities: ["TEXT", "AUDIO"] — required for function calling.
      The model emits text during tool-call reasoning; audio-only mode causes
      a server-side empty-output error on tool turns.
    - Function calling is sequential (model waits for tool response before continuing).

    Note on Gemini 3.1 Flash Live Preview:
    That model only supports ["AUDIO"] modality which is incompatible with function
    calling (causes 1007 errors). Switch back to 3.1 once it supports TEXT+AUDIO.
    """
    return {
        "setup": {
            "model": GEMINI_MODEL,
            "generationConfig": {
                # TEXT + AUDIO required: model emits text during tool-call reasoning.
                # Audio-only causes "model output must contain either output text
                # or tool calls" errors from the server when the model reasons.
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Aoede"}}},
                # Disable dynamic thinking (2.5 model default) — thinking-only turns
                # with no actual output trigger the empty-output server error.
                "thinkingConfig": {"thinkingBudget": 0},
            },
            "systemInstruction": {
                "parts": [
                    {
                        "text": (
                            "You are a helpful personal voice assistant. You have access to tools to "
                            "create todo tasks, log spending transactions, and update cash balances. "
                            "Always use these tools when asked. Keep your verbal responses concise and natural."
                        )
                    }
                ]
            },
            "tools": [
                {
                    "functionDeclarations": [
                        {
                            "name": "create_todo_task",
                            "description": "Create a new todo task/item for the user.",
                            "parameters": {
                                "type": "OBJECT",
                                "properties": {
                                    "title": {
                                        "type": "STRING",
                                        "description": "The title or text of the todo task.",
                                    },
                                    "due_date": {
                                        "type": "STRING",
                                        "description": "Optional due date in YYYY-MM-DD format (e.g. '2026-05-29').",
                                    },
                                    "priority": {
                                        "type": "STRING",
                                        "description": "The priority, one of 'low', 'medium', or 'high'.",
                                    },
                                },
                                "required": ["title"],
                            },
                        },
                        {
                            "name": "log_spending_transaction",
                            "description": "Record/log a new spending transaction (expense).",
                            "parameters": {
                                "type": "OBJECT",
                                "properties": {
                                    "amount": {
                                        "type": "STRING",
                                        "description": "The transaction amount as a string (e.g., '14.99').",
                                    },
                                    "category_name": {
                                        "type": "STRING",
                                        "description": "The name of the spending category (e.g., 'food', 'utilities', 'shopping').",
                                    },
                                    "description": {
                                        "type": "STRING",
                                        "description": "Description of what the money was spent on.",
                                    },
                                },
                                "required": ["amount", "category_name", "description"],
                            },
                        },
                        {
                            "name": "log_cash_balance",
                            "description": "Record/update cash balance for an investing account.",
                            "parameters": {
                                "type": "OBJECT",
                                "properties": {
                                    "account_name": {
                                        "type": "STRING",
                                        "description": "The name of the brokerage or bank account (e.g., 'Brokerage Cash').",
                                    },
                                    "balance": {
                                        "type": "STRING",
                                        "description": "The cash balance amount as a string (e.g. '1200.50').",
                                    },
                                    "currency": {
                                        "type": "STRING",
                                        "description": "The currency code (e.g. 'USD', 'EUR', 'GBP').",
                                    },
                                },
                                "required": ["account_name", "balance", "currency"],
                            },
                        },
                    ]
                }
            ],
        }
    }


async def _connect_gemini(gemini_url: str) -> tuple:
    """
    Connect to the Gemini Live API using GEMINI_MODEL.
    Returns (websocket, context_manager) on success.
    Raises RuntimeError if connection or setup fails.
    """
    logger.info("connecting_to_gemini", model=GEMINI_MODEL)
    ws_conn = websockets.connect(gemini_url)
    ws = await ws_conn.__aenter__()

    try:
        setup_message = _build_setup_message()
        await ws.send(json.dumps(setup_message))

        first_msg_raw = await asyncio.wait_for(ws.recv(), timeout=8.0)
        first_msg = json.loads(first_msg_raw)

        if "setupComplete" not in first_msg:
            raise RuntimeError(f"Unexpected setup response from Gemini: {first_msg}")

        logger.info("gemini_ws_setup_completed", model=GEMINI_MODEL)
        return ws, ws_conn

    except Exception:
        with suppress(Exception):
            await ws_conn.__aexit__(None, None, None)
        raise


async def _handle_gemini_message(
    msg: dict, client_ws: WebSocket, gemini_ws, user_id: int, workspace_id: int
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
        await client_ws.send_json({"type": "error", "message": error_msg})
        return
    # ── serverContent ────────────────────────────────────────────────────────
    server_content = msg.get("serverContent")
    if server_content:
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

            result = await execute_agent_tool(name, args, user_id, workspace_id)

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


async def run_agent_session(client_ws: WebSocket, user_id: int, workspace_id: int):
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        logger.error("gemini_api_key_missing")
        await client_ws.send_json({
            "type": "error",
            "message": "Gemini API key is not configured on the server.",
        })
        await client_ws.close(code=4002)
        return

    gemini_url = f"{GEMINI_LIVE_URL}?key={api_key}"
    decoder = AudioDecoder()
    await decoder.start()

    gemini_ws = None
    ws_context_manager = None

    try:
        gemini_ws, ws_context_manager = await _connect_gemini(gemini_url)
        logger.info("gemini_session_active", model=GEMINI_MODEL)

        # ── Background: stream decoded PCM → Gemini ───────────────────────────
        async def pcm_to_gemini_loop():
            try:
                while True:
                    # 2048 bytes of 16kHz 16-bit mono PCM ≈ 64ms of audio
                    chunk = await decoder.read_pcm_chunk(2048)
                    if not chunk:
                        await asyncio.sleep(0.01)
                        continue

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
                    await _handle_gemini_message(msg, client_ws, gemini_ws, user_id, workspace_id)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("gemini_to_client_loop_error", error=str(e))

        pcm_task = asyncio.create_task(pcm_to_gemini_loop())
        gemini_task = asyncio.create_task(gemini_to_client_loop())

        # ── Main loop: read client messages ──────────────────────────────────
        try:
            while True:
                message = await client_ws.receive()

                if "bytes" in message:
                    # Encoded audio (WebM/Opus etc.) — ffmpeg decodes to PCM
                    await decoder.send_encoded_chunk(message["bytes"])

                elif "text" in message:
                    client_msg = json.loads(message["text"])
                    msg_type = client_msg.get("type")

                    if msg_type == "text":
                        content = client_msg.get("content", "")
                        # Gemini 3.1: use realtimeInput for live text (not clientContent)
                        await gemini_ws.send(json.dumps({"realtimeInput": {"text": content}}))
        except WebSocketDisconnect:
            logger.info("client_websocket_disconnected")
        finally:
            pcm_task.cancel()
            gemini_task.cancel()
            await asyncio.gather(pcm_task, gemini_task, return_exceptions=True)

    except Exception as e:
        logger.error("gemini_live_session_error", error=str(e))
        with suppress(Exception):
            await client_ws.send_json({
                "type": "error",
                "message": f"Connection to Gemini Live API failed: {e}",
            })
    finally:
        await decoder.close()
        if ws_context_manager is not None:
            with suppress(Exception):
                await ws_context_manager.__aexit__(None, None, None)
