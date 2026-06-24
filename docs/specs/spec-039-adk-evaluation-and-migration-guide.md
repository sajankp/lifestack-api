# Google ADK Evaluation & Voice-First Multi-Agent Migration Guide

**Date:** 2026-06-24
**Context:** Lifestack voice agent (spec-021) currently uses a raw WebSocket bridge to Gemini Live API. This document evaluates Google Agent Development Kit (ADK) as a potential replacement and provides a concrete migration path for when multi-agent, voice-first interaction becomes the target architecture.

---

## Table of Contents

1. [Current Voice Agent Architecture](#1-current-voice-agent-architecture)
2. [What Google ADK Is (and Isn't)](#2-what-google-adk-is-and-isnt)
3. [Feature-by-Feature Comparison](#3-feature-by-feature-comparison)
4. [Should You Migrate Now?](#4-should-you-migrate-now)
5. [When ADK Becomes the Right Call](#5-when-adk-becomes-the-right-call)
6. [Multi-Agent Voice Architecture Vision](#6-multi-agent-voice-architecture-vision)
7. [Migration Strategy: Phase-by-Phase](#7-migration-strategy-phase-by-phase)
8. [Migration Risks and Mitigations](#8-migration-risks-and-mitigations)
9. [Dependency and Compatibility Considerations](#9-dependency-and-compatibility-considerations)
10. [Testing Strategy for Migration](#10-testing-strategy-for-migration)
11. [Decision Framework](#11-decision-framework)

---

## 1. Current Voice Agent Architecture

### 1.1 What Exists Today

The Lifestack voice agent (spec-021, Phase 1) is a custom WebSocket bridge implemented in ~750 lines of Python backend code and ~766 lines of React frontend code.

**Backend (`app/capture/`):**

```
capture/
  agent.py    (~758 LOC)  — WebSocket bridge, Gemini Live API, audio decode, session management
  tools.py    (~300 LOC)  — AgentTools class, 8 tool functions wrapping domain services
  router.py   (~100 LOC)  — WebSocket endpoint, cookie auth, workspace resolution
```

**Frontend (`src/components/`):**

```
VoiceAgentWidget.tsx   (~766 LOC)  — Floating copilot panel, MediaRecorder, WebSocket client,
                                      audio playback queue, transcript display
VoiceAgentFailureAlert.test.tsx     — Error display component tests
```

### 1.2 Data Flow

```
Browser (MediaRecorder WebM/Opus)
    │
    ▼  WebSocket binary frames
FastAPI WS endpoint (/v1/capture/agent/ws)
    │
    ├── ffmpeg subprocess (WebM/Opus → PCM 16kHz 16-bit mono)
    │       │
    │       ▼  base64-encoded PCM chunks
    │   Gemini Live API (wss://generativelanguage.googleapis.com)
    │       │
    │       ├── Audio response (24kHz PCM) → binary frames → Browser AudioContext
    │       ├── Transcript text → JSON → Browser transcript panel
    │       └── Tool calls → execute_agent_tool() → tool response → Gemini
    │
    └── CaptureSessionLimiter (frame bytes, total bytes, duration, text chars)
```

### 1.3 Key Custom Components

| Component | What It Does | LOC | ADK Equivalent? |
|---|---|---|---|
| `AudioDecoder` | ffmpeg subprocess: WebM/Opus → PCM 16kHz | ~55 | **None** — ADK expects raw PCM input |
| `CaptureSessionLimiter` | Enforces frame/session/time/text limits | ~30 | **None** — must re-implement |
| `_connect_gemini()` | 3-modality fallback (TEXT+AUDIO → TEXT → AUDIO) | ~80 | **None** — must re-implement |
| `_build_setup_message()` | Gemini Live setup payload with tool declarations | ~210 | **Replaced** — ADK auto-generates from Agent config |
| `execute_agent_tool()` | Manual toolCall parse → dispatch → toolResponse | ~45 | **Replaced** — ADK auto-executes tools |
| `_handle_gemini_message()` | Parse serverContent/toolCall/error messages | ~85 | **Replaced** — ADK yields typed events |
| `run_agent_session()` | 3 concurrent async tasks + lifecycle management | ~120 | **Partially replaced** — `run_live()` manages WS internally |
| `AgentTools` | 8 tool functions wrapping domain services | ~300 | **Kept** — wrapped as `FunctionTool` instances |

**ADK replaces ~340 LOC** (setup message, tool dispatch, message parsing, WS lifecycle).
**ADK does NOT replace ~415 LOC** (audio decode, session limits, modality fallback, auth, tool implementations).

### 1.4 Current Strengths

- **Full observability.** Granular structlog events at every decision point: `gemini_ws_setup_completed`, `gemini_modality_rejected`, `capture_session_limit_exceeded`, `tool_execution_failed`, `gemini_to_client_loop_error`, etc.
- **Defensive resilience.** 3-attempt modality fallback handles Gemini model changes. Session limiter prevents resource abuse. Error messages are sanitized before reaching clients.
- **Zero external framework dependency.** Only `websockets` library. Upgrade cadence controlled by you, not by Google's framework release schedule.
- **MCP-ready tool design.** Spec-021 Section 9 explicitly notes: "Tool functions are designed as decoupled, standalone Python functions... to allow the same registry and function interfaces to be exposed as MCP tools in the next stage."

---

## 2. What Google ADK Is (and Isn't)

### 2.1 Architecture

Google ADK is an **open-source agent framework**, not a cloud service. It is installed via standard package managers:

```bash
pip install google-adk       # Python (primary)
npm install @anthropic-ai/adk  # TypeScript also available
```

- **GA since May 2026** (ADK 2.0). Currently at **v2.3.0** (June 18, 2026).
- **~68 releases** with roughly bi-weekly cadence — the API surface is still shifting.
- Available in Python, TypeScript, Go, Java, and Kotlin.
- Python requires **3.10+** (3.11+ for 2.0 features). Lifestack uses 3.13, so compatible.
- ADK 2.0 introduced breaking changes from 1.x including a graph-based Workflow Runtime execution engine.

### 2.2 Core Abstractions for Streaming

| ADK Concept | What It Does |
|---|---|
| `Agent` | Declares model, system instructions, and tools in constructor |
| `FunctionTool` | Wraps a Python function as an auto-executable tool |
| `Runner` | Orchestrates agent execution |
| `run_live()` | Async generator managing bidirectional Gemini Live WebSocket |
| `LiveRequestQueue` | Upstream message channel (`send_content()`, `send_realtime()`) |
| `SessionService` | Session state management (in-memory or DB-backed) |

### 2.3 What ADK Handles Automatically

1. **Tool execution.** Detects `toolCall` events in streaming responses, executes matching `FunctionTool` instances, formats and sends `toolResponse` — all without manual intervention.
2. **WebSocket lifecycle.** `run_live()` manages the Gemini connection, handles reconnection, and yields typed events as an async generator.
3. **Setup message generation.** Tool declarations are auto-generated from Python function signatures and docstrings.
4. **Multi-agent orchestration.** Built-in support for routing between sub-agents, agent delegation, and shared session state.

### 2.4 What ADK Does NOT Handle

1. **Audio format conversion.** ADK requires 16-bit PCM at 16kHz mono input and outputs 24kHz mono. No codec support. The official docs explicitly warn: *"ADK does not perform audio format conversion."*
2. **Session resource limits.** No frame byte limits, cumulative byte limits, session duration caps, or text size limits.
3. **Modality fallback.** No retry strategy for model modality changes.
4. **WebSocket authentication.** No cookie-based auth, CSRF, or workspace resolution. The transport layer (your FastAPI WebSocket endpoint) remains yours.
5. **Custom observability.** ADK has its own logging, but does not expose hooks for granular structlog instrumentation at every decision point.

### 2.5 Known Issues (as of June 2026)

- **Dependency version conflicts** with FastAPI/Starlette reported (GitHub issues #2657, #3173). Lifestack pins `starlette>=1.3.1` and `fastapi>=0.135.0` for security patches — ADK may conflict.
- **Mixing built-in + custom tools** in the same agent can produce errors.
- **Callback/LongRunningTool support** in streaming mode is not yet complete.
- **`LiveRequestQueue` must never be reused** across sessions — each `run_live()` call requires a fresh queue.
- **Audio model architecture choice** affects capabilities: "native audio" (end-to-end, AUDIO only) vs "half-cascade" (native input + TTS output, supports TEXT+AUDIO).

---

## 3. Feature-by-Feature Comparison

| Dimension | Current (Raw WebSocket) | With ADK | Winner |
|---|---|---|---|
| **Tool execution** | Manual parse + dispatch map + response formatting | Auto-detected, auto-executed, auto-formatted | **ADK** |
| **WS lifecycle** | 3 concurrent async tasks, manual cancellation | `run_live()` manages internally | **ADK** |
| **Setup payload** | 210-line manual JSON construction | Auto-generated from Agent config | **ADK** |
| **Audio handling** | ffmpeg subprocess (yours) | Still yours — ADK has no codec | **Tie** |
| **Session limits** | Custom `CaptureSessionLimiter` | Must re-implement on top of ADK | **Current** |
| **Modality fallback** | Custom 3-attempt strategy | Must re-implement on top of ADK | **Current** |
| **Auth/RBAC** | Cookie-based WS auth with workspace resolution | Not provided — still yours | **Tie** |
| **Observability** | Granular structlog at every decision point | ADK's internal logging (less control) | **Current** |
| **Multi-agent routing** | Not supported (single agent) | Built-in agent orchestration | **ADK** |
| **MCP tool reuse** | Designed for MCP reuse (spec-021 §9) | MCPToolset built-in | **ADK** |
| **Provider portability** | Gemini-only (hardcoded) | Gemini-primary, abstraction layer exists | **ADK** |
| **Dependency footprint** | `websockets` only | `google-adk` + transitive deps (potential conflicts) | **Current** |
| **API stability** | Stable (you control) | Bi-weekly releases, still maturing | **Current** |
| **Total LOC** | ~750 (full control) | ~400 (framework dep + custom layers) | **ADK** (marginally) |
| **Time to onboard new dev** | Read 750 LOC of custom code | Learn ADK framework + read custom layers | **Current** |

**Score: Current wins 5, ADK wins 5, Ties 3.** The advantages are in different dimensions — ADK wins on abstraction and multi-agent; Current wins on control, observability, and stability.

---

## 4. Should You Migrate Now?

**No.** The cost-benefit does not justify migration today.

### Why Not

1. **The hard parts stay.** Audio decoding (ffmpeg), session limits, modality fallback, auth, and all 8 tool implementations (~415 LOC) remain regardless. ADK only replaces ~340 LOC of bridge code.

2. **You'd lose custom resilience.** The 3-modality fallback strategy has already saved you when Gemini changed supported modalities (per the code comments about Gemini 3.1 Flash Live Preview). ADK doesn't provide this.

3. **Dependency risk is high.** ADK went GA 6 weeks ago. Bi-weekly releases mean the API surface is still shifting. Known dependency conflicts with FastAPI/Starlette could force you to downgrade security-pinned packages.

4. **The current implementation works.** It's tested (capture tests + E2E), instrumented (structlog), and handles edge cases. Migration introduces regression risk for marginal gain.

5. **Single-agent doesn't need an orchestration framework.** ADK's strongest value proposition (multi-agent routing) is irrelevant for a single voice agent with 8 tools.

---

## 5. When ADK Becomes the Right Call

ADK becomes justified when **two or more** of these conditions are true:

| Condition | Why It Matters |
|---|---|
| **Multi-agent orchestration** | You want specialized sub-agents (spending agent, investing agent, todo agent) with voice-based routing between them |
| **Voice-first interaction replaces text** | The voice agent becomes the primary UI, not a sidebar widget — requires richer session state, conversation history, and context management |
| **MCP server integration** | You want the voice agent to consume external MCP tools alongside your domain tools — ADK's `MCPToolset` handles this natively |
| **Provider portability** | You want to swap between Gemini Live, OpenAI Realtime, or future providers without rewriting the agent layer |
| **ADK stabilizes** | The framework reaches a cadence of monthly (not bi-weekly) releases, dependency conflicts are resolved, and streaming callbacks are complete |
| **Team grows** | New developers join who know ADK but not your custom WebSocket bridge — framework familiarity reduces onboarding time |

**Estimated timeline for these conditions:** 6-12 months from now, aligning with Phase 2 (AI and Integrations) in your architecture roadmap.

---

## 6. Multi-Agent Voice Architecture Vision

This section describes the target state when voice-based multi-agent interaction fully replaces text-based interaction.

### 6.1 Agent Topology

```
                         ┌─────────────────────┐
                         │   Router Agent       │
                         │   (Voice-first)      │
                         │                      │
                         │   Listens to user    │
                         │   Decides which      │
                         │   specialist to      │
                         │   delegate to        │
                         └─────┬───┬───┬────────┘
                               │   │   │
              ┌────────────────┘   │   └────────────────┐
              ▼                    ▼                     ▼
    ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
    │  Todo Agent      │  │  Spending Agent  │  │  Investing Agent │
    │                  │  │                  │  │                  │
    │  Tools:          │  │  Tools:          │  │  Tools:          │
    │  - create_todo   │  │  - log_txn       │  │  - log_cash      │
    │  - list_todos    │  │  - list_txns     │  │  - list_holdings │
    │  - update_todo   │  │  - budget_check  │  │  - get_summary   │
    │  - delete_todo   │  │  - category_mgmt │  │  - get_perf      │
    │  - list_due      │  │                  │  │                  │
    └─────────────────┘  └─────────────────┘  └─────────────────┘
```

### 6.2 Why Multi-Agent Over Single Agent

The current single agent has 8 tools. As Lifestack grows, the tool count will expand:

| Module | Current Tools | Future Tools (estimated) |
|---|---|---|
| Todo | 6 (create, list, get, update, delete, list_due) | 10+ (recurring, search, tags, bulk ops) |
| Spending | 1 (log_transaction) | 8+ (list, budgets, analytics, categories, recurring, accounts) |
| Investing | 1 (log_cash_balance) | 10+ (holdings, performance, prices, lookthrough, snapshots) |
| Dashboard | 0 | 3+ (summary, notifications, weekly) |
| Export/Import | 0 | 4+ (create, status, download, import) |

A single agent with 35+ tools will suffer from:
- **Tool confusion** — LLM struggles to select the right tool from a large set
- **Prompt bloat** — system instruction grows with every tool description
- **Slow response** — model takes longer to reason over more function declarations
- **Context pollution** — unrelated tool schemas consume context window

Multi-agent routing solves this: the Router Agent classifies intent ("I spent $20 on lunch" → Spending Agent), the specialist receives only its own tools and domain context, and the response routes back through the Router Agent's voice.

### 6.3 Voice-First Interaction Model

When voice fully replaces text:

```
Current (Text-primary, voice-sidebar):
  Browser SPA → REST API → Domain services
  Browser SPA → Voice widget → WS → Gemini → Tools → Domain services

Future (Voice-primary):
  Browser SPA → Voice interface → WS → Router Agent → Specialist Agent → Tools → Domain services
  Browser SPA → REST API → Domain services (fallback for batch operations)
```

Key changes:
- **Voice becomes the primary input channel**, not a convenience overlay
- **Conversational state** must persist across turns (ADK's `SessionService`)
- **Agent responses drive UI updates** (tool results trigger React Query invalidation — already working today)
- **Multi-turn reasoning** becomes critical ("Log my lunch... actually make it dinner instead")
- **Conversation history** needs to survive session reconnects

---

## 7. Migration Strategy: Phase-by-Phase

### Phase 0: Preparation (No ADK Yet)

**Goal:** Make the current implementation ADK-ready without actually introducing ADK.

**Timeline:** Can start immediately, 1-2 sprints.

#### Step 0.1: Extract tool registry from agent.py

Currently, tool declarations are embedded in `_build_setup_message()` as a 210-line JSON blob. Extract them into a standalone registry:

```python
# app/capture/tool_registry.py

from dataclasses import dataclass
from typing import Callable, Any

@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., Any]

def get_tool_registry() -> list[ToolDefinition]:
    """Standalone tool registry — reusable by raw WS, ADK, and MCP."""
    return [
        ToolDefinition(
            name="create_todo_task",
            description="Create a new todo or reminder for the user.",
            parameters={...},
            handler=None,  # Bound at runtime with AgentTools instance
        ),
        # ... remaining 7 tools
    ]
```

**Why:** This decouples tool definitions from the transport layer. The same registry can later feed ADK's `FunctionTool`, MCP tool declarations, or the raw WebSocket setup message.

#### Step 0.2: Fix error leakage in tools.py

Replace all `str(e)` in client-facing responses with generic messages (as noted in the architecture audit, SEC-C1). This is a prerequisite for both security and ADK migration, since ADK's auto-execution would surface the same `str(e)` values.

```python
# Before (6 occurrences):
return {"status": "error", "message": str(e)}

# After:
return {"status": "error", "message": "An internal error occurred."}
```

#### Step 0.3: Extract CaptureSessionLimiter to standalone module

Move `CaptureSessionLimiter` from `agent.py` to `app/capture/limits.py`. This makes it reusable regardless of whether the underlying agent framework is raw WebSocket or ADK.

#### Step 0.4: Extract modality fallback to standalone module

Move `_connect_gemini()` and its 3-attempt modality strategy to `app/capture/connection.py`. This is unique operational knowledge that must survive any framework migration.

**After Phase 0, the file structure becomes:**

```
capture/
  agent.py              — Orchestration only (slimmed to ~200 LOC)
  tools.py              — AgentTools class (unchanged)
  tool_registry.py      — Standalone tool definitions
  limits.py             — CaptureSessionLimiter
  connection.py         — Gemini connection with modality fallback
  router.py             — WebSocket endpoint (unchanged)
```

---

### Phase 1: ADK Introduction (Parallel Path)

**Goal:** Add ADK as an alternative agent backend behind a feature flag, without removing the raw WebSocket implementation.

**Timeline:** 1-2 sprints after Phase 0. Only start when ADK's dependency conflicts with FastAPI/Starlette are resolved.

**Prerequisites:**
- ADK releases stable version without FastAPI/Starlette conflicts
- Phase 0 extraction is complete
- ADK streaming callbacks (before/after tool execution) are documented and stable

#### Step 1.1: Install ADK with version pinning

```toml
# pyproject.toml
dependencies = [
    # ... existing
    "google-adk>=2.4.0,<3.0.0",  # Pin to stable minor
]
```

Verify no dependency conflicts:
```bash
uv lock --check
uv run pip-audit
```

#### Step 1.2: Create ADK agent definition

```python
# app/capture/adk_agent.py

from google.adk import Agent, FunctionTool
from app.capture.tool_registry import get_tool_registry

def create_lifestack_agent(user_timezone: str = "UTC") -> Agent:
    """Create an ADK Agent with Lifestack tools."""
    tools = []
    for tool_def in get_tool_registry():
        tools.append(FunctionTool(func=tool_def.handler))

    return Agent(
        name="lifestack_voice_agent",
        model="models/gemini-2.5-flash-native-audio-latest",
        instruction=_build_system_instruction(user_timezone),
        tools=tools,
    )
```

#### Step 1.3: Create ADK-backed session runner

```python
# app/capture/adk_session.py

from google.adk import Runner, InMemorySessionService, LiveRequestQueue
from app.capture.adk_agent import create_lifestack_agent
from app.capture.limits import CaptureSessionLimiter

async def run_adk_agent_session(
    client_ws: WebSocket,
    user_id: int,
    workspace_id: int,
    user_timezone: str = "UTC",
):
    agent = create_lifestack_agent(user_timezone)
    session_service = InMemorySessionService()
    runner = Runner(agent=agent, app_name="lifestack", session_service=session_service)
    limiter = CaptureSessionLimiter.from_settings()
    decoder = AudioDecoder()
    await decoder.start()

    session = await session_service.create_session(
        app_name="lifestack",
        user_id=str(user_id),
    )

    live_queue = LiveRequestQueue()

    try:
        # Upstream: client audio → decode → ADK queue
        async def upstream_loop():
            while True:
                message = await client_ws.receive()
                limiter.validate_client_message(message)
                if message.get("bytes"):
                    pcm = await decoder.decode(message["bytes"])
                    live_queue.send_realtime(pcm)
                elif message.get("text"):
                    parsed = json.loads(message["text"])
                    if parsed.get("type") == "text":
                        live_queue.send_content(parsed["content"])

        # Downstream: ADK events → client
        async def downstream_loop():
            async for event in runner.run_live(
                session_id=session.id,
                live_request_queue=live_queue,
            ):
                # ADK yields typed events — forward to client
                if hasattr(event, 'server_content'):
                    # Audio and transcript
                    ...
                elif hasattr(event, 'tool_call'):
                    await client_ws.send_json({
                        "type": "tool_call",
                        "name": event.tool_call.name,
                        "arguments": event.tool_call.args,
                    })

        await asyncio.gather(upstream_loop(), downstream_loop())
    finally:
        await decoder.close()
```

#### Step 1.4: Feature flag in router

```python
# app/capture/router.py

@router.websocket("/agent/ws")
async def websocket_agent_endpoint(websocket: WebSocket):
    user_id, workspace_id = await authenticate_ws(websocket)
    await websocket.accept()

    if settings.CAPTURE_USE_ADK:
        from app.capture.adk_session import run_adk_agent_session
        await run_adk_agent_session(websocket, user_id, workspace_id, ...)
    else:
        await run_agent_session(websocket, user_id, workspace_id, ...)
```

```python
# app/config.py
CAPTURE_USE_ADK: bool = False  # Feature flag for ADK migration
```

#### Step 1.5: Validate parity

Run the existing E2E capture tests (`capture.spec.ts`) against both paths. Both must pass before proceeding.

---

### Phase 2: Multi-Agent Introduction

**Goal:** Introduce specialized sub-agents with a voice-first Router Agent.

**Timeline:** 2-4 sprints after Phase 1 stabilizes. Aligns with Phase 2 (AI and Integrations) in the architecture roadmap.

**Prerequisites:**
- ADK path is stable and passing all tests
- Tool count has grown to the point where single-agent performance degrades
- Product decision to invest in voice-first UX

#### Step 2.1: Define specialist agents

```python
# app/capture/agents/todo_agent.py
todo_agent = Agent(
    name="todo_specialist",
    model="models/gemini-2.5-flash-native-audio-latest",
    instruction="You manage todos and reminders. ...",
    tools=[create_todo, list_todos, get_todo, update_todo, delete_todo, list_next_due],
)

# app/capture/agents/spending_agent.py
spending_agent = Agent(
    name="spending_specialist",
    model="models/gemini-2.5-flash-native-audio-latest",
    instruction="You manage spending transactions and budgets. ...",
    tools=[log_transaction, list_transactions, check_budget, ...],
)

# app/capture/agents/investing_agent.py
investing_agent = Agent(
    name="investing_specialist",
    model="models/gemini-2.5-flash-native-audio-latest",
    instruction="You manage investment holdings and cash balances. ...",
    tools=[log_cash_balance, list_holdings, get_summary, get_performance, ...],
)
```

#### Step 2.2: Create Router Agent

```python
# app/capture/agents/router_agent.py
router_agent = Agent(
    name="lifestack_router",
    model="models/gemini-2.5-flash-native-audio-latest",
    instruction=(
        "You are the Lifestack voice assistant. Route user requests to the "
        "appropriate specialist: todo_specialist for tasks and reminders, "
        "spending_specialist for expenses and budgets, investing_specialist "
        "for holdings and portfolio queries. If the request spans multiple "
        "domains, handle sequentially."
    ),
    sub_agents=[todo_agent, spending_agent, investing_agent],
)
```

#### Step 2.3: Session state management

Replace `InMemorySessionService` with a database-backed service for conversation persistence:

```python
from google.adk import DatabaseSessionService

session_service = DatabaseSessionService(
    db_url=settings.DATABASE_URL.replace("+asyncpg", ""),
)
```

Or implement a custom `SessionService` backed by your existing PostgreSQL + SQLAlchemy stack for tighter integration.

---

### Phase 3: Voice-First UI

**Goal:** Voice becomes the primary interaction channel.

**Timeline:** 3-6 months after Phase 2. Aligns with product strategy decision.

#### Changes Required

| Component | Current State | Voice-First State |
|---|---|---|
| VoiceAgentWidget | Floating sidebar copilot (56px launcher) | Full-screen voice interface with contextual UI |
| REST API | Primary data channel | Fallback for batch/export operations |
| React Query | Drives UI from REST responses | Drives UI from both REST + voice agent tool responses |
| Navigation | URL-based routing | Voice-driven ("show me my spending") + URL fallback |
| Conversation history | Not persisted | Stored in ADK SessionService, survives reconnects |
| Multi-turn context | Single session, no memory | Agent remembers context within conversation |
| Error handling | Per-page error states | Voice agent announces errors and suggests recovery |

#### Frontend Migration

```typescript
// Current: Small floating widget
<VoiceAgentWidget />

// Future: Full voice interface with contextual panels
<VoiceInterface>
  <ConversationPanel />        {/* Always visible */}
  <ContextualDataPanel />      {/* Shows relevant data based on conversation */}
  <QuickActionBar />           {/* Voice-suggested actions */}
</VoiceInterface>
```

---

## 8. Migration Risks and Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | ADK dependency conflicts with FastAPI/Starlette security pins | High (known issue) | High | Don't start Phase 1 until conflicts are resolved upstream. Pin ADK to tested version. |
| R2 | ADK streaming API changes between versions | Medium | High | Pin to minor version (`>=2.4,<2.5`). Run capture E2E tests on every ADK update. |
| R3 | Loss of observability during migration | Medium | Medium | Implement custom ADK event handlers that emit structlog events matching current format. |
| R4 | Multi-agent routing adds latency | Medium | Medium | Benchmark Router Agent overhead vs direct tool dispatch. Set P95 latency budget. |
| R5 | Session state migration from in-memory to DB | Low | Medium | Start with `InMemorySessionService` in Phase 1. Migrate to DB-backed in Phase 2 only when conversation persistence is needed. |
| R6 | Tool execution behavior differs between raw and ADK paths | Medium | High | Run both paths in parallel (feature flag) with identical E2E tests until parity is confirmed. |
| R7 | Gemini model changes break ADK differently than raw WS | Low | High | Keep the raw WS path available as a rollback for 3+ months after ADK becomes default. |
| R8 | ADK `LiveRequestQueue` reuse bug causes session corruption | Low | High | Documented ADK constraint. Create fresh queue per `run_live()` call. Add defensive check. |

---

## 9. Dependency and Compatibility Considerations

### 9.1 Current Backend Dependencies (Relevant)

```toml
fastapi>=0.135.0
starlette>=1.3.1        # Pinned for security
websockets>=16.0
google-generativeai>=0.8.6
```

### 9.2 ADK Dependency Chain

```
google-adk
  ├── google-genai          (Gemini client)
  ├── google-adk-tools      (built-in tool implementations)
  ├── pydantic>=2.x         (compatible)
  ├── starlette             (potential version conflict)
  └── various google-* libs
```

### 9.3 Compatibility Matrix

| Dependency | Lifestack Pin | ADK Requirement | Compatible? |
|---|---|---|---|
| Python | 3.13 | >=3.10 (3.11+ for 2.0) | Yes |
| FastAPI | >=0.135.0 | Varies (conflicts reported) | **Check per version** |
| Starlette | >=1.3.1 | Varies (conflicts reported) | **Check per version** |
| Pydantic | >=2.12.0 | >=2.x | Yes |
| websockets | >=16.0 | Not required (ADK uses own WS) | N/A |

### 9.4 Pre-Migration Compatibility Check

Before starting Phase 1, run:

```bash
# Add ADK to pyproject.toml
uv add google-adk>=2.4.0

# Check for conflicts
uv lock --check

# Verify security pins aren't downgraded
uv tree | grep -E "starlette|fastapi|cryptography"

# Run security audit
uv run --with pip-audit pip-audit

# Run full test suite
uv run pytest -q
```

If any security-pinned package is downgraded, **do not proceed**. File a bug against ADK and wait for resolution.

---

## 10. Testing Strategy for Migration

### 10.1 Parity Testing (Phase 1)

The capture E2E test (`lifestack-e2e/e2e/capture.spec.ts`) must pass identically against both the raw WebSocket and ADK paths.

```bash
# Test raw WS path (current)
CAPTURE_USE_ADK=false docker compose up
npx playwright test capture.spec.ts

# Test ADK path (new)
CAPTURE_USE_ADK=true docker compose up
npx playwright test capture.spec.ts
```

### 10.2 New Tests for ADK Path

| Test | What It Validates |
|---|---|
| `test_adk_tool_auto_execution` | ADK correctly dispatches to `AgentTools` methods |
| `test_adk_tool_error_sanitization` | ADK path doesn't leak `str(e)` to client |
| `test_adk_session_limits` | `CaptureSessionLimiter` enforced on ADK path |
| `test_adk_modality_fallback` | Modality fallback works through ADK |
| `test_adk_session_isolation` | Workspace scoping maintained through ADK sessions |
| `test_adk_concurrent_sessions` | Multiple ADK sessions don't interfere |
| `test_adk_graceful_degradation` | ADK failure falls back to raw WS (if configured) |

### 10.3 Performance Benchmarks

Measure before and after migration:

| Metric | Acceptable Range |
|---|---|
| Time to first audio response | < 2 seconds |
| Tool execution round-trip (tool_call → tool_response) | < 500ms |
| Connection setup time | < 3 seconds |
| Memory per session | < 50 MB |
| P95 end-to-end latency | < 1.5x current baseline |

---

## 11. Decision Framework

Use this checklist to decide when to proceed with each phase:

### Phase 0 (Extract + Harden) — Start Now

- [x] Decision: Always beneficial, no ADK dependency
- [ ] Error leakage fixed (SEC-C1)
- [ ] Tool registry extracted
- [ ] Session limiter extracted
- [ ] Connection strategy extracted
- [ ] Existing tests still pass

### Phase 1 (ADK Parallel Path) — Start When:

- [ ] ADK version exists without FastAPI/Starlette conflicts
- [ ] ADK streaming callback API is documented and stable
- [ ] Phase 0 extraction is complete
- [ ] `uv lock --check` passes with ADK added
- [ ] `pip-audit` shows no new vulnerabilities
- [ ] Full test suite passes with ADK in dependencies

### Phase 2 (Multi-Agent) — Start When:

- [ ] ADK path passes all capture E2E tests
- [ ] Tool count has grown beyond 15 (single-agent performance concern)
- [ ] Product decision made to invest in voice-first UX
- [ ] ADK multi-agent orchestration is GA and stable
- [ ] Latency benchmarks acceptable with Router Agent overhead

### Phase 3 (Voice-First UI) — Start When:

- [ ] Multi-agent routing is stable in production for 1+ month
- [ ] Conversation persistence (DB-backed SessionService) is implemented
- [ ] Frontend voice interface design is approved
- [ ] Accessibility audit for voice-primary interaction completed
- [ ] Fallback to text/REST verified for all critical paths

---

## Summary

| Question | Answer |
|---|---|
| Should we migrate to ADK now? | **No.** Current implementation works, ADK is too young, dependency conflicts exist. |
| What should we do now? | **Phase 0:** Extract tool registry, session limiter, and connection strategy into standalone modules. Fix error leakage. |
| When should we add ADK? | **When** dependency conflicts are resolved AND multi-agent orchestration is needed. Estimated 6-12 months. |
| Will ADK improve voice quality? | **No.** Audio handling stays identical. ADK adds no codec or audio processing. |
| Will ADK improve tool execution? | **Yes.** Automated tool dispatch removes ~100 LOC of manual glue code. |
| What's the biggest ADK win? | **Multi-agent orchestration.** When tool count exceeds 15+, routing to specialist agents will significantly improve accuracy and response time. |
| What's the biggest ADK risk? | **Dependency conflicts** with security-pinned packages, and **loss of custom observability** during migration. |
| What's the migration effort? | Phase 0: 1-2 sprints. Phase 1: 1-2 sprints. Phase 2: 2-4 sprints. Phase 3: 3-6 months. |

---

*This document should be revisited quarterly as ADK matures and Lifestack's voice agent requirements evolve.*
