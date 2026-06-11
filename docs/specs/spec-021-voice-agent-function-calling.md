# Feature Spec: Voice Agent with Function Calling
**Status:** Implemented (Phase 1)
**Spec ID:** 021

Implementation note (2026-06-11): `/v1/capture/agent/ws`, cookie-only WebSocket authentication, agent tool execution for todo/spending/investing cash balance flows, session/frame/text limits, sanitized provider errors, OpenAPI tag coverage, and capture tests are implemented. The WebRTC/media-server notes below are context only; future sequencing lives in the product roadmap.

## 1. Overview
This specification details the architecture and implementation of the **Voice Agent with Function Calling** feature. Traditional client-side speech-to-text (STT) and text-to-speech (TTS) browser APIs lose rich speech nuances (tone, emotion, pauses, emphasis). To provide a premium voice assistant, this feature streams raw audio directly to/from a live conversational LLM backend.

---

## 2. Roadmap

### Phase 1: WebSocket Audio Chunk Streaming (Current Implementation)
- **Frontend**: Captures microphone audio using the browser's `MediaRecorder` API and streams raw audio chunks over a WebSocket connection to FastAPI. Plays back LLM-generated audio chunks returned by the server. Browser-native STT/TTS are discarded. The voice interface is implemented as a persistent, floating copilot drawer/side-panel widget accessible across all views.
- **FastAPI Backend**: Exposes a WebSocket endpoint `/v1/capture/agent/ws` (following the standard module-subpath namespace). It forwards incoming client audio chunks to a live multimodal LLM API (such as the Gemini Live API or OpenAI Realtime WebSocket API), registers the local tool function registry, executes tools on behalf of the agent, and routes audio/text responses back to the client.
- **Voice Agent**: Exposes local services as tools. The LLM decides when to execute these tools based on the audio conversation.

### Phase 2/3: Production-Grade WebRTC Connection
- **Frontend**: Establishes a direct WebRTC peer connection to a media server (like LiveKit).
- **FastAPI**: Acts as the session orchestrator, handling authentication, token generation, tool definition exposure, state storage, and validation of actions.
- **Voice Agent**: Sits on the backend, joining the WebRTC room, listening to the user, calling FastAPI tools, and synthesising speech back to the peer connection.

Phase 2/3 are context only, not active backlog inside this spec.

---

## 3. Goals
- Expose existing backend services (`TodoService`, `TransactionService`, `InvestingService`) as tools (functions) to the LLM agent.
- Reuse existing services so that business logic and internal audit logging are not duplicated.
- Expose a WebSocket route on FastAPI to orchestrate audio streaming between the client and the live LLM API.
- Support real-time tool execution during voice conversation.
- Design tools as decoupled Python functions to allow direct reuse as Model Context Protocol (MCP) tools in the next stage.

---

## 4. API & Protocol Surface (Phase 1)

### WebSocket Connection
`WS /v1/capture/agent/ws`

#### Client-to-Server Messages
1. **Audio Chunk**: Binary message containing raw audio bytes from client microphone.
2. **Text / Control Message**: JSON message (e.g. to explicitly interrupt playback or send text-only updates).
```json
{
  "type": "text",
  "content": "Hello, can you help me?"
}
```

#### Server-to-Client Messages
1. **Audio Output**: Binary message containing audio bytes representing synthesized model speech.
2. **Event / Transcript Output**: JSON message containing real-time text transcription, tool execution status, or UI synchronization events.
```json
{
  "type": "transcript",
  "content": "I'm adding that todo for you..."
}
```
```json
{
  "type": "tool_call",
  "name": "create_todo_task",
  "arguments": {
    "title": "Buy groceries",
    "due_date": "2026-05-29"
  }
}
```
```json
{
  "type": "tool_response",
  "name": "create_todo_task",
  "status": "success",
  "entity_id": "uuid"
}
```

---

## 5. Tool Function Definitions
The LLM will be supplied with tool function schemas mapped to the following backend services:
1. `create_todo_task(title, due_date, priority)` -> maps to `TodoService.create_todo()`
2. `log_spending_transaction(amount, category_name, description)` -> maps to `TransactionService.create_transaction()`
3. `log_cash_balance(account_name, balance, currency)` -> maps to `InvestingService.create_cash_balance()`

---

## 6. Audit Logging
All audit logs are executed internally by the target service. The Agent Tool layer passes the standard `AuditLogger` instantiated from the workspace session, ensuring zero duplicate logging logic.

---

## 7. Security Considerations
- Validate user session authentication upon WebSocket handshake.
- Limit WebSocket session duration, single-frame bytes, cumulative session bytes, and text-message size with conservative environment-backed defaults.
- Return sanitized client-facing provider errors while logging raw provider failures server-side.
- Verify workspace-scoping and authorization of the user session on every tool function call.

---

## 8. Runtime Limits
- `CAPTURE_MAX_WS_FRAME_BYTES`: maximum single client audio frame, default `262144` bytes.
- `CAPTURE_MAX_SESSION_BYTES`: maximum cumulative client audio bytes per session, default `15728640` bytes.
- `CAPTURE_MAX_SESSION_SECONDS`: maximum voice session duration, default `300` seconds.
- `CAPTURE_MAX_TEXT_CHARS`: maximum text message size, default `4000` characters.

---

## 9. MCP (Model Context Protocol) Compatibility
Tool functions are designed as decoupled, standalone Python functions that do not depend on FastAPI dependencies or request context directly. This allows the same registry and function interfaces to be exposed as MCP tools in the next stage.
