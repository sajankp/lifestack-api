# Spec 061 — Voice Agent: Backdated Spending Transactions

**Status:** Implemented (api#126, merged 2026-07-07)
**Repos:** lifestack-api (capture layer only)
**Depends on:** spec-021 (voice agent function calling), spec-054 (mandatory transaction account), spec-055 (workspace awareness), spec-059 (voice agent usability)

## Motivation

The voice capture agent can log an expense, but it always stamps the
transaction with the server's current time:
`AgentTools.log_spending_transaction` hard-codes
`occurred_at=datetime.now(UTC)` (`app/capture/tools.py`), and neither the
tool signature nor the Gemini function declaration
(`app/capture/gemini_setup.py`) exposes a date argument. So even when the
user says *"log ₹500 for groceries **yesterday**"*, the spoken date is
silently dropped and the row is dated "now".

This is an implicit scope limitation, not a documented decision — and it is
**inconsistent with the rest of the surface**:

- Todos on the same voice surface already accept dates: `create_todo_task`
  takes `due_date`, and the system prompt instructs the model to resolve
  relative phrases ("today at 4 PM") into ISO 8601 date-times.
- The REST / manual UI path already accepts a user-chosen date:
  `TransactionCreate.occurred_at` is a required `datetime` with no
  now-only restriction.

Voice is the only entry point that cannot set a transaction date. This spec
brings it to parity by adding an **optional** occurrence date to the voice
spending tool.

## Changes (lifestack-api, capture layer only)

### 1. Tool: `AgentTools.log_spending_transaction`

Add an optional `occurred_at: str | None = None` argument (spoken as a
relative phrase or an ISO date/date-time by the user; the model resolves it
using the current-date + timezone facts already in the system prompt).

Parsing / normalization (new `_parse_occurred_at(value, user_timezone)`
helper, mirroring the todo `_parse_due_datetime` pattern but timezone-aware):

- **Omitted / blank** → `datetime.now(UTC)` (unchanged behavior; the common
  case stays a one-liner).
- **Full ISO date-time with offset** (`2026-07-03T13:00:00+05:30`) → parse and
  convert to UTC.
- **ISO date-time without offset** → interpret in the user's session timezone,
  then convert to UTC.
- **Bare date** (`YYYY-MM-DD`, e.g. from "yesterday", "on July 3rd") →
  **[DECISION A — noon-local]** interpret as **noon (12:00) in the user's
  session timezone**, then convert to UTC. Noon (not midnight) is deliberate: a
  bare date stamped at midnight UTC lands on the *previous* calendar day for
  western (negative-offset) users, corrupting day-grouping in analytics
  (spec-058 windows expenses by `occurred_at` local day). Noon-local is safely
  inside the intended day for every real-world offset.
- **Unparseable** → structured
  `{"status": "error", "message": "Invalid date. Use a day like 'yesterday' or an ISO date."}`
  so the agent asks one short question instead of failing.

Guardrail — **[DECISION B — reject future days, clamp same-day]:**

- If the resolved timestamp's **local calendar day is after the user's current
  local day** → return
  `{"status": "error", "message": "I can't log a spend for a future date."}`
  (you don't spend money in the future; this blocks "tomorrow", "next week").
- If the resolved timestamp is in the future but on the **current** local day
  (e.g. "today" resolved to noon-local while it is still morning) → **clamp to
  `datetime.now(UTC)`** rather than reject, so the natural "log X today" path
  never errors.
- **No lower bound.** Legitimately old catch-up entries ("I paid for this last
  month") are allowed, matching the REST path.

The resolved `occurred_at` is passed into the existing `TransactionCreate`
payload; all other resolution (category, fuzzy account per spec-059,
default-account fallback) is unchanged. The success dict gains
`"occurred_at": tx.occurred_at.isoformat()` so the agent can state the date
back to the user.

### 2. Gemini function declaration (`gemini_setup.py`)

Add an optional `occurred_at` property to the `log_spending_transaction`
declaration (kept **out** of `required`):

> `occurred_at` — "Optional occurrence date for the expense. Provide when the
> user states a past or relative day (e.g. 'yesterday', 'last Monday', 'on
> July 3rd') as an ISO date (`YYYY-MM-DD`) or full ISO date-time with UTC
> offset. Omit when the spend is happening now — the server defaults to the
> current time."

### 3. System prompt (`gemini_setup.py`)

Add one instruction to the spending block, reusing the current-date +
timezone facts already injected. It also makes the agent **aware of the
future-date block** ([DECISION B] surfaced to the model) so it explains the
limit up front instead of relying on a silent server rejection:

> "When the user states when a spend happened ('yesterday', 'last Monday', 'on
> the 3rd'), resolve it against the current date and the user's timezone and
> pass it as `occurred_at`. Omit `occurred_at` when the spend is happening now.
> State the date back to the user when you logged a past spend. You cannot log
> a spend for a future date — if the user names a future day, tell them and ask
> for the actual (past or current) date instead of calling the tool."

If the tool nonetheless returns the future-date error (defense in depth), the
agent relays the message rather than claiming success.

## Out of scope

- Editing/redating **existing** transactions by voice (no `update_transaction`
  voice tool). This spec only sets the date at creation.
- Investing / cash-balance dates — investing stays read-only on voice
  (spec-059).
- Any REST endpoint, schema, or migration change. `TransactionCreate` already
  accepts `occurred_at`; only the capture layer changes.
- lifestack-web / lifestack-e2e changes.
- **Retroactivity: N/A.** No historical snapshot/ledger rows are read or
  mutated. Each call creates one new spending-ledger row with a
  user-chosen date — normal creation, not a backfill.

## Test plan (api, Red first)

- `_parse_occurred_at`: omitted → ~now(UTC); bare date resolves to the correct
  UTC instant for a non-UTC timezone (`Asia/Kolkata` and a negative-offset zone
  like `America/Los_Angeles`) and lands on the intended local day; full-offset
  date-time → correct UTC; unparseable → error dict.
- `log_spending_transaction`: past date persists that `occurred_at`; omitted
  arg still stamps ~now; a future **day** returns the future-date error and
  writes no row; a same-day future instant clamps to now; success dict includes
  `occurred_at`.
- Declaration: `log_spending_transaction` exposes `occurred_at`, and it is not
  in `required`. System prompt asserts the backdating + future-block instruction.
- Full gate: `uv run pytest --cov=app -q` (coverage ≥ 80), `ruff check` +
  `ruff format`.

## Decisions (approved 2026-07-07)

- **[A]** Bare-date time-of-day = **noon in the user's timezone** (avoids
  day-drift for negative-offset users).
- **[B]** Future dates: **reject future days, clamp same-day-future to now**;
  the future-date limit is surfaced to the agent in the system prompt so it
  explains it up front rather than relying on the server rejection alone.
