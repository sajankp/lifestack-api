# Spec 059 — Voice Agent Usability: Fuzzy Spending Accounts, Read-Only Investing, Barge-In

**Status:** Implemented (maintainer directive, 2026-07-05)
**Repos:** lifestack-api, lifestack-web
**Depends on:** spec-021 (voice agent function calling), spec-054 (mandatory transaction account), spec-055 (workspace awareness)

## Motivation

Live usage of the voice capture agent surfaced three friction points:

1. **Account naming is too strict.** `log_spending_transaction` resolves
   `account_name` via exact, case-sensitive string equality
   (`AccountRepository.get_by_name`). Voice transcription almost never
   reproduces the stored casing, and colloquial references ("my wallet",
   "the card") fail outright, forcing the user to dictate exact account names.
2. **The agent cannot be interrupted.** Gemini Live emits
   `serverContent.interrupted` when its VAD detects the user talking over the
   model, but the bridge (`_handle_gemini_message`) ignores it, and the web
   client schedules decoded audio ahead of real time — so already-buffered
   speech plays to the end no matter what the user does.
3. **Investing mutations don't belong on voice.** The maintainer does not
   create investing entries (orders, cash balances) by voice; at most they ask
   for a summary. Keeping mutation tools on the voice surface adds risk and
   prompt/context noise (brokerage accounts injected into the prompt) for no
   benefit.

Additionally, `thinkingConfig.thinkingBudget` is pinned to 0, which measurably
hurts tool-argument quality on Flash-class models; the maintainer wants a
modest budget.

## Changes

### lifestack-api

1. **Fuzzy spending-account resolution** (capture layer only; REST endpoints
   unchanged). `AgentTools.log_spending_transaction` resolves `account_name`
   against **active, spending-eligible accounts** (every type except
   `brokerage`) in this order:
   1. normalized exact match (casefold + strip);
   2. unique containment match — the candidate account name contains the
      spoken name or vice versa (normalized);
   3. unique account-type match — the spoken name equals an `AccountType`
      value (e.g. "wallet", "card"), and exactly one active spending-eligible
      account has that type.

   Ambiguity (≥2 candidates at any step) returns a structured error with
   `needs_account: true` and `candidates: [names]` so the model asks one short
   disambiguation question. No match returns an error carrying
   `available_accounts: [names]`. The default-account path (no `account_name`
   given) is unchanged from spec-054/055.
2. **Investing becomes read-only on voice.** Remove `place_stock_order` and
   `log_cash_balance` from the tool dispatch, the Gemini function
   declarations, the system prompt, and `AgentTools` (methods + tests
   deleted). Add read-only `get_investing_summary` (no required parameters)
   that reuses `InvestingSummaryService.get_summary` and returns portfolio
   value, holdings count, cash total, and reporting currency as strings.
   REST endpoints for orders/cash balances are untouched.
3. **Prompt/context hygiene.** `_fetch_workspace_context` injects only active
   non-brokerage accounts. System instruction: drop investing-mutation
   guidance; instruct the model to map colloquial account references onto the
   injected account list and that the server matches fuzzily; tool description
   for `account_name` no longer demands the "exact" name.
4. **Barge-in forwarding.** `_handle_gemini_message` forwards
   `serverContent.interrupted` to the client as `{"type": "interrupted"}`.
5. **Thinking budget.** New setting `GEMINI_THINKING_BUDGET: int = 256`
   (env-overridable) replaces the hardcoded `"thinkingBudget": 0`. Set `0` in
   `.env` to restore the old behavior if the configured model rejects a
   non-zero budget.

### lifestack-web

6. **Interruption handling.** `VoiceAgentWidget` handles the new
   `interrupted` server message by clearing the scheduled audio queue
   (`clearAudioQueue`). Tapping the mic off (`stopRecording`) also clears the
   queue, so the user always has a local "shut up now" path even with the mic
   closed. (Starting to record already clears the queue.)

## Out of scope

- Cascade/push-to-talk architecture, ADK migration (spec-039 owns that
  decision), quota management.
- Fuzzy matching on REST endpoints or in other tools (`log_cash_balance` is
  deleted; category matching is unchanged from spec-055).
- lifestack-e2e changes: the WS bridge contract only gains an optional
  `interrupted` message type; existing flows are unaffected.
- Any schema or data change. **Retroactivity: N/A** — no ledger/snapshot
  write paths change behavior.

## Test plan

- api (Red first): fuzzy resolution — case-insensitive exact, containment,
  type-based, ambiguous → `needs_account` + candidates, no-match →
  `available_accounts`, brokerage excluded; dispatch no longer exposes
  `place_stock_order`/`log_cash_balance` and exposes `get_investing_summary`;
  declarations/prompt assertions updated; `interrupted` forwarded to client;
  setup message carries the configured thinking budget; workspace context
  omits brokerage accounts.
- web (Red first): new `VoiceAgentWidget` test — `interrupted` message stops
  scheduled audio sources; mic-off clears the queue.
- Full gates: api pytest + coverage ≥80, ruff; web vitest + coverage ≥70,
  `npm run build`, lint.
