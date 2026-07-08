# Spec-066: Capture Consolidation (one capture model, text-first, confirmation cards)

**Created:** 2026-07-08
**Status:** Approved — owner decisions recorded 2026-07-08; ready for implementation
**Scope:** multi-repo, user-facing — primarily `lifestack-web`; `lifestack-api` changes are additive-only (tool-result normalization, no REST or schema changes).
**Depends on:** spec-021 (voice agent function calling), spec-059 (voice usability), spec-061 (voice transaction date). Owner decisions (2026-07-08 — binding inputs, not proposals): capture is promoted to a first-class surface (nav/header entry, text-first, voice as a mode, confirmation cards); the voice widget stays but must be hideable, use a mic icon, phrase messages as outcomes, and connect only on explicit user action. Related: `docs/product/PRODUCT_STRATEGY_AND_ROADMAP.md` §2 (Voice/capture promoted to Secondary: "Universal input layer; text-first with voice mode") and its 2026-07-08 changelog entry.

---

## Problem

The app has three disconnected capture mechanisms, none of which shows the user what was created (verified 2026-07-08 against `lifestack-web main@84d3eb5` / `lifestack-api main@113d27f`; the header "+" buttons were since wired to real create modals by the 2026-07 UX hardening pass):

1. **`/capture` is an orphan page calling an endpoint that does not exist.** `CapturePage.tsx` is routed but linked from nowhere, and `src/services/capture.ts` POSTs to `/v1/capture` — a REST endpoint that was never implemented. Spec-018 (which defined it) is archived; `app/capture/` contains only the WebSocket agent (`WS /v1/capture/agent/ws`). Any submit on that page fails with a 404. The page also renders results as raw `JSON.stringify` and uses "Module hint / Amount hint" developer vocabulary.
2. **The voice widget narrates internals.** `VoiceAgentWidget.tsx` shows "Executed create_todo successfully", uses a Sparkles icon that doesn't communicate voice, cannot be hidden, and opens the Gemini Live WebSocket as soon as the panel is opened rather than when the user actually sends something.
3. **Capture never shows where things went.** Both paths invalidate caches correctly, but the user gets no confirmation card, no summary of the created record, and no link to it.

This contradicts the product thesis directly: the strategy rejects "a generic chatbot with loose files attached", yet the current widget — a floating chat bubble narrating tool calls, disconnected from the structured surfaces — is exactly that pattern. The differentiator to encode is: **every capture produces a structured record with a visible confirmation naming what was created, where it went, and a deep link.**

## Goals

- ONE capture model: a single capture surface, text-first, with voice as a mode of the same surface.
- Capture is in the app chrome: nav/header entry point plus a keyboard shortcut.
- Every successful capture renders a **confirmation card**: record type, human summary, destination module, deep link ("Added ₹450 'lunch' to Spending → view"). Never raw JSON, never tool-call narration.
- The voice mode honors the owner decisions above: mic icon, outcome-phrased messages, hideable, connects only on explicit user action.
- The orphaned `/capture` page and its dead REST service are retired.

## Non-goals

- New capture domains (health, journal, documents) — spec-018's future-extensions list stays future.
- Multi-item capture ("buy groceries and pay rent").
- ADK migration or transport changes — spec-039 territory; the Gemini Live WebSocket bridge is reused as-is.
- Mobile-native capture (Track 2).
- Persistent server-side capture history (see Open questions — recommended deferral).
- Any change to REST endpoints, schemas, or data. **Retroactivity: N/A** — no ledger/snapshot write path changes behavior.

## Solution

### A. One surface: the Capture panel (lifestack-web)

The existing `VoiceAgentWidget` evolves into a single **Capture panel** — the only capture surface in the app:

- **Entry points:** (1) a "Capture" item in the nav `Life` section (`layout/constants.ts` `NAV_LINKS`); (2) the existing floating launcher, re-iconed (mic/plus glyph instead of Sparkles); (3) a global keyboard shortcut (proposed: `Ctrl/Cmd+K` opens it with the text input focused; if `Ctrl+K` is wanted for a future command palette, fall back to `Ctrl/Cmd+J`). All three open the same panel component.
- **Text-first:** the panel opens with the text input focused. The mic is a mode toggle inside the panel, not a separate widget. Text submissions go over the existing WS (`{"type": "text", ...}` → Gemini `realtimeInput`), which already routes through the same tool registry as voice — no second backend path.
- **Lazy connection (owner decision):** opening the panel does NOT open the WebSocket. The connection is established on the first text submit or mic activation, and torn down per the existing session limits. The launcher never auto-connects.
- **Hideable (owner decision):** a "Show capture launcher" preference (persisted in `localStorage`, keyed by workspace id — same pattern as the first-run checklist dismissal). When hidden, the nav entry and keyboard shortcut still work; only the floating launcher disappears. The toggle lives in the panel's own overflow/settings corner and in Settings.
- **`/capture` retired outright:** the route, `CapturePage.tsx`, and `src/services/capture.ts` are all deleted — no redirect or compatibility shim (owner decision: don't preserve broken code). The service calls a REST endpoint that was never implemented, so nothing working is lost; the nav entry, launcher, and shortcut are the replacement, and a stale `/capture` URL falls through to the router's existing unknown-route handling.

### B. Confirmation-card contract

The WS server already emits `{"type": "tool_response", "name", "status", "entity_id", "result"}` per tool call. The contract this spec adds:

**API side (additive normalization, no new endpoints):** every *mutating* tool result in `AgentTools` MUST include:

| Field | Meaning | Example |
|---|---|---|
| `entity_type` | stable record-type discriminator | `todo`, `recurring_todo`, `transaction` |
| `entity_public_id` | the created/updated record's public id | uuid |
| `summary` | one human sentence, already localized to the record's own currency/date | `Added ₹450 'lunch' to Spending` |

`create_todo_task` and `log_spending_transaction` already return `entity_type` + `entity_public_id`; the work is auditing the remaining mutating tools (`create_recurring_todo`, `update_todo`, `delete_todo`) for the same shape and adding `summary` everywhere. Read-only tools (`list_todos`, `get_investing_summary`, …) are exempt — they feed the transcript, not cards. No REST contract changes.

**Web side:** a card registry keyed by `entity_type` maps each result to `{icon, module label, route}`:

| entity_type | Route |
|---|---|
| `todo` | `/todo` (+ highlight by public_id if the page supports it) |
| `recurring_todo` | `/todo` recurring section |
| `transaction` | `/spending` Transactions tab |

The panel renders `tool_response(status=success)` as a confirmation card (`summary` + "View →" link) and `status=error` as a plain-language failure line. `tool_call` events render nothing user-visible (at most a subtle "working…" state) — tool names and raw arguments never appear. Unknown `entity_type` falls back to a generic "Saved — view in app" card rather than JSON.

### C. Outcome-phrased transcript

The transcript area of the panel shows only: the user's own inputs (text or voice transcript), the model's spoken/text replies, and the confirmation/error cards from B. The current "Executed create_todo successfully" strings are removed. Errors keep the existing sanitized-provider-error behavior.

## Now vs. Proposed

| Aspect | Now | Proposed |
|---|---|---|
| Capture surfaces | 3 (orphan page, floating widget, wired "+" buttons) | 1 panel + the "+" buttons (which stay as direct-create shortcuts) |
| `/capture` page | 404s on submit, renders raw JSON | route, page, and dead service deleted |
| Result display | `JSON.stringify` / "Executed create_todo successfully" | confirmation card: summary + module + deep link |
| Discoverability | none (no nav entry, hidden shortcut-less widget) | nav entry + launcher + keyboard shortcut |
| WS connection | opens when panel opens | opens on first send / mic press |
| Widget dismissal | impossible | hideable launcher preference |
| Icon | Sparkles | mic/capture glyph |

## Test plan

- **api (Red first):** unit tests asserting every mutating tool result carries `entity_type`, `entity_public_id`, `summary` (shape test over the dispatch registry, so a future tool can't regress silently). Gates: `uv run pytest --cov=app` ≥ 80, ruff.
- **web (Red first):** panel tests — confirmation card rendered per `entity_type` with correct route; unknown type falls back generically; error `tool_response` renders failure line, never JSON; WS not opened on panel mount, opened on first submit (mock WS); hide preference persists per workspace; shortcut opens panel with input focused; no remaining references to the removed route/page/service. Gates: vitest ≥ 70 coverage, `npm run build`, lint.
- **e2e (`lifestack-e2e`):** `capture.spec.ts` currently intercepts `/capture/agent/ws` — update it to drive the panel (open via nav entry, send text, assert the confirmation card and deep link against the mocked WS frames). Keyboard-shortcut smoke assertion added.

## Rollout

No feature flag: the change replaces surfaces that are today either broken (page) or judged harmful to positioning (widget narration). The hide preference is the escape hatch for the launcher. Implementable in one web PR + one small api PR (tool-result normalization first, since the card contract consumes it).

## Owner decisions (2026-07-08)

1. **Capture history:** ✅ Defer — session-local transcript only; no server-side capture history in this spec.
2. **Keyboard shortcut:** ✅ Use `Ctrl/Cmd+K`.
3. **Hide preference scope:** ✅ `localStorage` per workspace now; promote to a synced user preference only if multi-device friction proves it necessary.
