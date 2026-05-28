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

GEMINI_LIVE_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent"


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

    try:
        async with websockets.connect(gemini_url) as gemini_ws:
            logger.info("gemini_ws_connected")

            # 1. Send initial setup message
            setup_message = {
                "setup": {
                    "model": "models/gemini-2.0-flash-exp",
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {
                            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Aoede"}}
                        },
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
            await gemini_ws.send(json.dumps(setup_message))

            # Background task to stream decoded PCM to Gemini
            async def pcm_to_gemini_loop():
                try:
                    while True:
                        # 2048 bytes of 16kHz 16-bit mono PCM is ~64ms of audio
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

            # Background task to stream Gemini responses to Client
            async def gemini_to_client_loop():
                try:
                    async for raw_msg in gemini_ws:
                        msg = json.loads(raw_msg)

                        # Handle serverContent
                        server_content = msg.get("serverContent")
                        if server_content:
                            model_turn = server_content.get("modelTurn")
                            if model_turn:
                                parts = model_turn.get("parts") or []
                                for part in parts:
                                    # Send transcript text
                                    text = part.get("text")
                                    if text:
                                        await client_ws.send_json({
                                            "type": "transcript",
                                            "content": text,
                                        })

                                    # Send binary audio chunk
                                    inline_data = part.get("inlineData")
                                    if inline_data:
                                        audio_b64 = inline_data.get("data")
                                        if audio_b64:
                                            audio_bytes = base64.b64decode(audio_b64)
                                            await client_ws.send_bytes(audio_bytes)

                        # Handle toolCall
                        tool_call = msg.get("toolCall")
                        if tool_call:
                            function_calls = tool_call.get("functionCalls") or []
                            for fc in function_calls:
                                call_id = fc.get("id")
                                name = fc.get("name")
                                args = fc.get("args") or {}

                                await client_ws.send_json({
                                    "type": "tool_call",
                                    "name": name,
                                    "arguments": args,
                                })

                                result = await execute_agent_tool(name, args, user_id, workspace_id)

                                await client_ws.send_json({
                                    "type": "tool_response",
                                    "name": name,
                                    "status": result.get("status", "success"),
                                    "entity_id": result.get("entity_public_id"),
                                    "result": result,
                                })

                                # Send result back to Gemini
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

                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error("gemini_to_client_loop_error", error=str(e))

            # Start background worker loops
            pcm_task = asyncio.create_task(pcm_to_gemini_loop())
            gemini_task = asyncio.create_task(gemini_to_client_loop())

            # Read client messages
            try:
                while True:
                    message = await client_ws.receive()

                    if "bytes" in message:
                        # Client audio chunk
                        await decoder.send_encoded_chunk(message["bytes"])

                    elif "text" in message:
                        # Client control/text message
                        client_msg = json.loads(message["text"])
                        msg_type = client_msg.get("type")

                        if msg_type == "text":
                            content = client_msg.get("content", "")
                            await gemini_ws.send(
                                json.dumps({
                                    "clientContent": {
                                        "turns": [{"role": "user", "parts": [{"text": content}]}],
                                        "turnComplete": True,
                                    }
                                })
                            )
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
