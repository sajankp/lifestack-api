# Spec-093: Voice Transaction Correction

**Status:** Implemented — pending deployment validation
**Scope:** `lifestack-api` voice/capture agent, authenticated MCP parity, plus focused capture/E2E coverage
**Depends on:** spec-054 mandatory transaction accounts, spec-061 voice transaction dates, spec-078 reconciliation, spec-090 capture idempotency

## Problem

The conversation agent can create and list spending transactions, but it cannot
correct an incorrectly captured amount, category, account, description, tags, or
date. The underlying `TransactionService` already supports workspace-scoped
updates and deletes with append-only audit snapshots and reconciliation-match
invalidation; the missing surface is the agent-facing lookup and mutation flow.

## Goals

- Let the agent find an existing spending transaction using natural-language
  clues without requiring the user to know its UUID.
- Let the agent update supported transaction fields after identifying one
  unambiguous record.
- Let the agent delete a transaction only after an explicit user confirmation.
- Preserve workspace isolation, account/category resolution, user timezone
  handling, audit history, and statement-reconciliation safety.
- Make every lookup, mutation, ambiguity, validation error, and replay outcome
  observable in the persistent capture log without logging secrets.
- Cover the complete path from model tool declaration through dispatch,
  persistence, audit behavior, and a browser-level capture test.

## Non-goals

- Editing investment orders, transfers, recurring spending rules, or imported
  source files through voice.
- Changing the transaction database schema or REST transaction contract.
- Allowing arbitrary SQL, broad workspace search, or autonomous financial edits.
- Changing the existing future-dated transaction policy outside the voice tool.

## Agent tools

The authenticated MCP server exposes the complete current voice-tool surface
under the same tool names, with `workspace_id` added as an MCP argument and
`mcp:read`/`mcp:write` enforced per capability. This includes the existing todo,
spending, health, and read-only finance tools as well as the correction tools
below. MCP derives the effective user timezone from the authenticated user and
reuses the same `AgentTools` implementations rather than maintaining a second
business-logic path.

MCP also exposes `list_workspaces` and the passive `lifestack://me/workspaces`
resource. A client should use these first when the host has not already bound a
workspace. The authorization flow carries the web user's selected/default
workspace into the MCP access token when available; if there is exactly one
active workspace it is marked current automatically. Every subsequent
workspace-scoped call still verifies membership and scope server-side.

The bounded reference data is available both as the
`get_workspace_reference_data` read tool and the
`lifestack://workspaces/{workspace_id}/reference-data` resource template. The
resource is read-only and excludes brokerage accounts.

### `find_spending_transactions` (read-only)

Search a bounded local-date range in the user's persisted timezone. Supported
filters are `from_day`, `to_day`, `amount`, `search`, `category_name`, and
`account_name`; return at most 25 candidates with public ID, amount, category,
description, account, tags, occurrence time, and source metadata needed to
identify the record. An empty or over-broad query must remain bounded and return
an error asking for another clue rather than silently selecting a record.

### `update_spending_transaction`

Update one transaction by public ID. Supported voice fields are amount,
category, account, description, tags, and occurrence date. Transaction type and
import provenance are not editable through voice. Category and account names
are resolved using the same workspace-scoped rules as transaction creation.

The system instruction must require this sequence:

1. Find the candidate when the user did not provide a public ID.
2. Read back the current record and proposed change.
3. Obtain clear confirmation for amount, account, category, date, or deletion
   changes; harmless text-only corrections may proceed when explicitly stated.
4. Call the mutation only after confirmation and report the returned result.

The tool returns the updated record, changed fields, and a concise summary.
It must never accept a model-invented UUID or claim success for an error.

### `delete_spending_transaction`

Delete one transaction by public ID, only after the same explicit confirmation
rule. The result includes the deleted record's identifying summary. The service
must retain its existing behavior of breaking any statement match before
deletion and writing an append-only delete audit event.

## Safety and consistency

- Voice binds every operation to the workspace selected during WebSocket
  authentication. MCP may receive a discovered workspace ID from its client,
  but always derives the user from the bearer token and verifies membership and
  scope before execution; an unknown or unauthorized workspace is rejected.
- Use the persisted user timezone for local-day lookup and date interpretation.
- Reuse `TransactionUpdate`, `TransactionService.update_transaction_with_details`,
  and `TransactionService.delete_transaction` so existing validation, audit,
  and reconciliation behavior remains authoritative.
- Reuse capture replay deduplication for successful write calls.
- Do not include raw credentials, cookies, tokens, or account numbers in logs.
  Capture logs may include user-authored descriptions and amounts, so they stay
  feature-gated and require sanitization before export.
- Failed tool-call logs should include a bounded error code/category and safe
  message, not only `status=error`, to make production diagnosis possible.

## Acceptance criteria

- The model declaration, dispatcher, implementation, and write-tool dedup set
  contain the same new tool names.
- A user can correct a uniquely matched transaction's amount, category,
  account, description, tags, and date through the conversation flow.
- Ambiguous or missing matches cause a clarification response and no mutation.
- Delete without explicit confirmation causes no mutation.
- Update/delete are workspace-scoped and produce the expected audit snapshot.
- Amount/date edits clear statement reconciliation matches as they do through
  the existing REST service.
- A resumed duplicate write is suppressed and returns the original result.
- Focused API tests cover success, ambiguity, invalid values, workspace
  isolation, audit/reconciliation behavior, and replay suppression.
- The capture E2E test covers lookup → confirmation → update and verifies the
  persisted transaction and visible tool response.

## Proposed implementation locations

- `app/capture/tools.py`: lookup and mutation wrappers.
- `app/capture/gemini_setup.py`: declarations and confirmation instructions.
- `app/capture/agent.py`: dispatch and write-tool registration/logging.
- `app/tests/capture/test_agent.py`: declaration, tool, and behavior tests.
- `lifestack-e2e/e2e/capture.spec.ts`: browser/WebSocket flow.
