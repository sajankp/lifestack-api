# Spec-090: Voice Capture Session Clearing and Tool-Call Idempotency

**Created:** 2026-07-22
**Status:** Approved (implementation) — 2026-07-22
**Depends on:** spec-079 (voice capture production hardening, Stage B transport resilience), spec-055 (workspace context injection)

## Problem

Spec-079 Stage B added session resumption: Gemini periodically emits a
resumption handle (`agent.py` `sessionResumptionUpdate` handling), the client
stores the latest one, and every **automatic** reconnect after a drop carries it
back via `?resume=<handle>` (`VoiceAgentWidget.tsx` `connectWebSocket`) so
Gemini restores the prior conversation context.

Two gaps follow from that design:

### 1. Duplicate tool-call execution window on resume

Tool execution is fire-then-respond: when Gemini emits a `toolCall`, the server
executes the side effect (`execute_agent_tool` — e.g.
`log_spending_transaction`, `create_todo_task`) and **then** sends the
`toolResponse` back to Gemini. If the connection drops after the side effect
commits but before Gemini receives the response, a resumed session restores a
conversation state in which the function call is still unanswered. The model
can then re-emit the call (or re-derive it from restored context), and the
server will execute it again — `execute_agent_tool` has no idempotency guard,
and Gemini's per-call `id` (`fc["id"]`) is used only to address the response
payload, never checked against already-executed calls.

Consequence: a dropped connection at the wrong moment can double-log a
spending transaction, double-create a todo, or double-log a weight/medication
event, silently.

Compounding detail: each WebSocket session mints a fresh `session_id`
(`uuid.uuid4().hex` in `run_agent_session`), including resumed ones — so the
capture log cannot even correlate a resumed session with its predecessor,
making after-the-fact duplicate detection harder than it needs to be.

### 2. No user-facing "clear session" provision

The user cannot deliberately start a fresh conversation:

- The **only** path that drops the resumption handle is the manual retry after
  reconnection has already failed `MAX_RECONNECT_ATTEMPTS` times
  (`VoiceAgentWidget.tsx` `handleRetry` — "Manual retry starts a fresh
  session"). Closing the widget unmounts and clears state (Layout keeps
  WebSocket/history/handle state inside the widget precisely so unmount
  clears it), but that is teardown, not an in-session control.
- Automatic reconnects **always** resume. There is no way to opt out, and no
  age limit on the stored handle — a stale restored context is exactly what
  makes the model re-derive already-completed actions.

## Solution

### A. Client: explicit "new session" control + handle hygiene

1. Add a **New session** affordance to the voice widget (visible while
   connected or reconnecting) that: clears `resumptionHandleRef`, clears the
   message history, closes the current WebSocket with an intentional-close
   flag, and reconnects without `?resume=`. Emit a `capture_session_cleared`
   analytics event.
2. Expire the stored handle by age: record the timestamp when a
   `session_resumption` message arrives; on reconnect, discard handles older
   than a configurable TTL (`CAPTURE_RESUME_HANDLE_MAX_AGE_SECONDS`-driven via
   the existing settings surface, client default mirroring the server value).
   Gemini handles have a bounded server-side lifetime anyway; presenting an
   expired handle risks a failed resume where a clean fresh session would have
   worked.
3. On `session_resumption` with `resumable: false` semantics already handled
   server-side, no client change needed (server only forwards usable handles).

### B. Server: tool-call idempotency guard for write tools

1. Classify the dispatch table in `execute_agent_tool` into **write** tools
   (`create_todo_task`, `create_recurring_todo`, `log_spending_transaction`,
   `log_weight`, `log_medication_event`, `update_todo`, `delete_todo`) and
   **read** tools (`get_*`, `list_*`). Read tools bypass the guard.
2. Before executing a write tool, compute a dedup key:
   `(workspace_id, user_id, tool_name, sha256(canonical_json(args)))`. If the
   same key executed successfully within the last
   `CAPTURE_TOOL_DEDUP_WINDOW_SECONDS` (default 120), skip execution and
   return the **original** result with `status: "duplicate_suppressed"` so
   Gemini receives a coherent function response and the client can render a
   "already done" note instead of a second confirmation.
3. Store the guard in-process (per-worker dict with timestamp eviction) —
   sufficient because a resumed session lands on the same single-container
   deployment (1 GB Oracle VPS, one API container); a Redis-backed store is
   explicitly out of scope until the deployment is horizontal.
4. Log Gemini's `call_id` and the dedup key in `_log_capture_turn` entries,
   and add a `resumed: bool` + `resume_of_session_id` field to the session
   start path (client sends the prior `session_id` alongside `?resume=`) so
   the capture log can correlate resumed sessions — this is what makes Part C
   measurable on future data.
5. Rationale for content-keying rather than keying on Gemini's `call_id`
   alone: it is **unverified** whether Gemini reuses the same `call_id` when a
   resumed session re-emits a pending call (Part C measures this). Content
   keying within a short window catches both id-reuse and model-re-derivation
   duplicates; the window is short enough that a genuine repeated intent
   ("log another 100 lunch") minutes later is unaffected. Identical
   legitimate repeats *within* the window are the accepted trade-off, stated
   in Out of scope.

### C. Measurement on real production data (blocked on data hand-off)

The owner will provide the production capture turn log (the JSONL written to
`CAPTURE_TURN_LOG_PATH`, bind-mounted on the VPS) for offline analysis.
Analysis questions, in order:

1. **Duplicate incidence:** within each `session_id` (and across
   temporally-adjacent sessions for the same workspace, as a proxy for resumes
   until B.4's correlation fields exist), how often do identical
   `(tool, args)` write calls occur within 120 s? This sizes the real-world
   frequency of the bug and validates the default window.
2. **Resume behavior:** for sessions that follow a `capture_session_ended`
   with a drop reason, does the model re-emit pending calls, and does the
   re-emitted call carry the same Gemini `call_id`? (Determines whether B.2's
   content key can be tightened to an id check later.)
3. **Spec-079 Stage C input:** the same log's `assistant_transcript` +
   `tool_call` entries ground the eval set in real usage instead of synthetic
   adversarial cases. Caveat, stated for expectation-setting: per spec-079 Q4
   the log carries **no raw user utterance text** (input transcription is
   metered-cost-gated), so it can ground tool-routing/argument-extraction
   eval cases fully, but transcription-accuracy analysis only indirectly via
   assistant replies.

Findings land as a dated addendum to this spec; if measured incidence is zero
across the available history, Part B still ships (the failure window is
structural), but the default dedup window may be shortened.

## 2026-07-22 Addendum — Part C findings (production log analyzed)

The owner provided the production capture log (122 JSONL entries,
2026-07-13 → 2026-07-22, 68 write tool-calls, 26 Stage-B sessions). Findings,
which **revise Part B's design** (revisions below supersede the body where
they conflict):

**1. Measured duplicate incidence: ≥15% of write executions.**
10/68 write calls were exact duplicates (identical money-relevant args) of a
call executed ≤15 min earlier — 8 of them cross-session. A further ~10
near-duplicates repeat the same amount with drifted descriptive fields. The
2026-07-20 17:43 five-transaction batch was executed **three times** (original
`0851bd87`, replay `c8f9b049` at +47 s, replay `f87ebe07` at +8 min). On
2026-07-22 a batch replayed at +12.8 min (`50a256b0`) and another at +37 min
(`b1a3ad51`).

**2. Replay sessions have an unmistakable structural signature.**
All six replay sessions (`c8f9b049`, `f87ebe07`, `5e7afedb`, `ae69da39`,
`50a256b0`, `b1a3ad51`) are zero-duration, contain **only** tool calls (no
user or assistant transcripts), and fire their whole burst at t=0 — Gemini
re-emits pending function calls immediately on resume, before any new user
turn.

**3. Replays can drift, so exact content-keying is insufficient.**
`b1a3ad51` replayed `25bfb31f`'s batch with descriptions rewritten
("auto" → "Cab ride") and `account_name`/`occurred_at` added. Amount +
tool + date survived drift; descriptions and categories did not (a replay
even recategorized "tea" from Snacks to Eat out).

**4. Replay delays reach ~37 min, so a 120 s window is far too short.**
The +12.8 min and +37 min replays are inconsistent with the client's 8 s-capped
reconnect backoff; the likely mechanism is a suspended device (screen
off/sleep) whose reconnect timer fires on wake with a stale handle. Part A.2's
handle age-expiry directly kills this class.

**5. The model cannot self-detect duplicates.** In `a9c1a454` the user asked
whether a transaction was logged twice; the assistant checked its restored
conversation context (not the DB) and wrongly answered no.

**6. Correction to the Part C.3 caveat:** the log DOES contain
`user_transcript` entries (8, from 2026-07-17) — input transcription was
enabled at least briefly — so real-utterance eval cases for spec-079 Stage C
are partially available after all, though sparse.

**Part B revisions (supersede B.2/B.3 defaults):**

- **Scope the guard to the replay signature, not a global window:** dedup
  applies to write tool-calls that arrive in a session **before any user
  input on that connection** (text or audio frame). Calls after real user
  activity are never suppressed — this exempts genuine same-envelope repeats
  (e.g. the legitimate two ₹90 metro rides in `118f2cd5`) and same-session
  intentional repeats, both observed in the data.
- **Fuzzy dedup key:** `(workspace_id, user_id, tool_name, amount,
  occurred_at)` — the fields that survived replay drift — checked against
  executions in the last `CAPTURE_TOOL_DEDUP_WINDOW_SECONDS`, default
  **2700** (45 min, covering the observed +37 min worst case with margin).
  With the guard scoped to pre-user-input calls, the wide window carries no
  false-positive cost for normal usage.
- **Multiplicity-aware:** the prior execution count is respected — if the
  original envelope legitimately contained N identical calls, a replay
  re-emitting N is fully suppressed, not N−1 of them.

## Backend impact

- `app/capture/agent.py`: dedup guard around `execute_agent_tool` write
  dispatch; `call_id`/dedup-key/resume-correlation fields in capture log
  entries.
- `app/config.py`: `CAPTURE_TOOL_DEDUP_WINDOW_SECONDS` (default 2700 per the
  2026-07-22 addendum),
  `CAPTURE_RESUME_HANDLE_MAX_AGE_SECONDS`.
- `app/capture/router.py`: accept optional `prev_session` query param for
  resume correlation.
- No schema/Alembic impact — guard is in-process, log is JSONL.
- Tests: unit tests for the dedup guard (hit, miss, window expiry, read-tool
  bypass, error results not cached), router param plumbing, log field
  presence. Golden replay fixtures — scrubbed reconstructions of the four
  observed incident shapes in the production capture-log JSONL schema — live
  in `docs/specs/spec-090-replay-scenarios/` (see its README for the
  must-suppress / must-execute contract per scenario); guard tests replay
  them verbatim.

## Frontend impact

- `src/components/VoiceAgentWidget.tsx`: New-session control, handle-age
  expiry, `prev_session` param on resume, rendering of
  `duplicate_suppressed` tool responses.
- Tests: widget tests for clear-session behavior and suppressed-duplicate
  rendering.

## Out of scope

- Cross-process/Redis-backed dedup store (single-container deployment today).
- Exactly-once semantics for reads or for Gemini-side conversation state.
- Suppressing genuinely intended identical repeats inside the dedup window —
  accepted trade-off; the window default keeps this rare and Part C data can
  tune it.
- Input transcription / raw-utterance logging changes (spec-079 Q4 metering
  decision stands).
- Any change to the eval scorer or Stage C accuracy bar itself (that remains
  spec-079's contract; Part C here only feeds it data).
