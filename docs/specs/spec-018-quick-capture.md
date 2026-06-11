# Feature Spec: Quick Capture API
**Status:** Archived - Deferred to Roadmap
**Spec ID:** 018

Archive note (2026-06-11): this proposal is not an active implementation spec. The shipped capture surface is the voice-agent WebSocket plus tool-calling workflow. This file is retained as historical context; future capture sequencing lives in the product roadmap until a focused implementation slice is selected.

## 1. Overview
The README's Stage 2 (Capture Layer) calls for "fast capture for todo, spending, and journal entries" and "simple voice-first input for core actions." This spec introduces a unified capture endpoint that accepts lightweight, low-structure input and routes it to the appropriate module. It is the backend foundation for mobile quick-add, voice transcription results, and future AI-assisted parsing.

This builds on:
- Spec 003 (spending): transaction creation
- Spec 008 (investing): holding management (read-only capture reference)
- Todo module: task creation
- Spec 004 (audit logging): capture events

## 2. Goals
- Provide a single `POST /v1/capture` endpoint for rapid entry.
- Accept freeform text with optional structured hints (amount, category, due date).
- Route to the correct module based on input signals.
- Minimize required fields — capture should be easier than forgetting.
- Support explicit module targeting when the user knows the destination.
- Return the created entity's public_id and module for client-side navigation.
- Foundation for voice and AI-assisted capture in later stages.

## 3. Non-Goals (for this slice)
- Natural language parsing or AI classification (rule-based routing only in V1).
- Voice transcription (capture receives text; transcription is a client concern).
- Batch capture (one item per request).
- Capture for investing module (too structured for quick entry).
- Capture for health or journal modules (not yet implemented).
- Undo/edit from capture response (use module-specific PATCH endpoints).
- Offline queue or client-side buffering protocol.

## 4. API Surface

### Capture Endpoint
`POST /v1/capture`

#### Request Body
```json
{
  "text": "Buy groceries tomorrow",
  "module": null,
  "hints": {
    "amount": null,
    "category": null,
    "due_date": null,
    "priority": null,
    "type": null
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Freeform input (1–500 chars) |
| `module` | enum | no | Explicit target: `todo`, `spending`. If null, system routes automatically. |
| `hints` | object | no | Structured metadata to assist routing and entity creation |
| `hints.amount` | string | no | Decimal amount (signals spending) |
| `hints.category` | string | no | Category name or public_id |
| `hints.due_date` | string | no | ISO date for todo due date |
| `hints.priority` | string | no | `low`, `medium`, `high` for todos |
| `hints.type` | string | no | `income` or `expense` for spending |

#### Response
```json
{
  "captured": true,
  "module": "todo",
  "entity_public_id": "uuid",
  "entity_type": "task",
  "parsed": {
    "title": "Buy groceries",
    "due_date": "2026-05-26"
  }
}
```

#### Error Response
If routing fails because the request contains conflicting signals that cannot be resolved safely:
```json
{
  "captured": false,
  "reason": "conflicting_input",
  "suggestions": ["todo", "spending"],
  "message": "Capture contained conflicting module signals. Please specify module or add stronger hints."
}
```
HTTP 422 with RFC 7807 problem detail wrapping.

## 5. Routing Logic (V1 — Rule-Based)

### Priority Order
1. **Explicit module** — if `module` is provided, route directly.
2. **Amount hint present** — route to spending (with `type` defaulting to `expense`).
3. **Keyword signals** — simple pattern matching on `text`:
   - Spending signals: currency symbols (`$`, `£`, `₹`), "spent", "paid", "bought", "cost", number patterns
   - Todo signals: "todo", "task", "remind", "buy", "call", "email", "fix", "do"
4. **Default** — if no signal matches, route to `todo` (tasks are the catch-all and the lowest-friction fallback).
5. **Conflict handling** — only return `422` when signals conflict in a way that would likely create the wrong entity without user intent.

### Routing Decision Table
| Condition | Target | Confidence |
|-----------|--------|------------|
| `module = "spending"` | spending | explicit |
| `module = "todo"` | todo | explicit |
| `hints.amount` present | spending | high |
| Text contains currency/number pattern | spending | medium |
| Text contains action verb | todo | medium |
| No signal | todo | low (default) |
| Strong todo and spending signals with no explicit module | 422 | conflicting |

## 6. Module Integration

### Todo Capture
When routed to todo, the capture service calls the todo service to create a task:
- `title`: extracted from `text` (strip routing signals)
- `due_date`: from `hints.due_date` or parsed date expressions ("tomorrow", "next Monday")
- `priority`: from `hints.priority`, default `medium`

### Spending Capture
When routed to spending, the capture service calls the spending service:
- `description`: from `text`
- `amount`: from `hints.amount` or extracted number from text
- `type`: from `hints.type`, default `expense`
- `category_id`: resolved from `hints.category` (match by name or public_id), default to "Other" category
- `occurred_at`: current timestamp

### Date Expression Parsing (V1 — minimal)
Support a small set of relative date expressions:
- "today" → current date
- "tomorrow" → current date + 1
- "next week" → current date + 7 (Monday)
- ISO dates (YYYY-MM-DD) passed through directly

Complex NLP date parsing is a Stage 3 concern.

### Ambiguity Rule
The product principle for this spec is to keep capture easier than forgetting. That means:
- low-confidence or underspecified input defaults to `todo`
- explicit or high-confidence spending signals create spending entries
- only genuinely conflicting input should return `422`

Examples:
- `"follow up on insurance"` -> `todo`
- `"spent 250 on groceries"` -> `spending`
- `"buy groceries tomorrow"` -> `todo`
- `"pay rent tomorrow 25000"` with no `module` and no category/type hint -> `422` if both task-like and spending-like signals are equally strong

## 7. Architecture Placement

```
app/
  capture/
    __init__.py
    router.py       # POST /v1/capture
    service.py      # routing logic, module dispatch
    schemas.py      # CaptureRequest, CaptureResponse
    parser.py       # text signal extraction, date parsing
```

The capture service is an **orchestration layer** — it calls existing module services (todo, spending) for entity creation. It does not duplicate creation logic.

## 8. Audit Events
- `capture_routed` — capture received and routed to module (module: `capture`)
- `capture_failed` — capture could not be routed (module: `capture`)

Audit detail includes: `text` (first 100 chars), `routed_to`, `confidence`, `hints_provided`.

## 9. Configuration
- `CAPTURE_ENABLED`: feature flag, default `true`.
- `CAPTURE_DEFAULT_MODULE`: fallback when routing is low-confidence, default `todo`.
- `CAPTURE_MAX_TEXT_LENGTH`: max input length, default `500`.

## 10. Security Considerations
- Input text is sanitized (strip HTML/script tags) before processing.
- Rate limiting applied (shared with auth rate limits from existing middleware).
- `text` field in audit logs is truncated to prevent PII over-logging.
- No eval/exec of text content — routing is pattern-match only.

## 11. Test Plan
- **Unit tests:**
  - Routing logic for each signal type (explicit, amount, keywords, default)
  - Date expression parsing
  - Category resolution (by name, by public_id, fallback)
  - Text sanitization
  - Amount extraction from text patterns
- **Integration tests:**
  - Capture → todo creation roundtrip
  - Capture → spending creation roundtrip
  - Explicit module override works
  - Low-confidence input defaults to todo
  - Conflicting input returns 422 with suggestions
  - Audit events emitted
  - Workspace scoping enforced

## 12. Acceptance Criteria
- `POST /v1/capture` endpoint accepts freeform text and optional hints.
- Rule-based routing correctly dispatches to todo or spending.
- Explicit `module` field overrides automatic routing.
- Created entities are valid and accessible via their module's standard endpoints.
- Low-confidence inputs default to `todo`.
- Conflicting inputs return a helpful error with module suggestions.
- Date expressions ("tomorrow", "next week") parsed for todo due dates.
- Audit trail records all capture attempts with routing decisions.
- Input sanitization prevents injection vectors.

## 13. Migration
- No database migration needed (capture uses existing module tables).
- New module directory `app/capture/` with router registered in `main.py`.

## 14. Future Extensions (not in this spec)
- AI-powered routing and entity extraction (Stage 3).
- Voice transcription integration.
- Multi-item capture ("Buy groceries and pay rent").
- Capture for health, journal, and document modules.
- Confidence scoring with user confirmation for low-confidence routes.
