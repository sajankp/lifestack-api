# Spec 101 — Batch Spend Writes, Idempotency/dry_run, and Date-Range Spending Queries

**Status:** Draft (retroactive) — **pending Sajan's approval.** The implementation was written
before any approval of this spec was recorded (kanban card `t_8ec81992`); this document
describes what was built and tested. Do not mark `Implemented` or merge until approved.

## Problem
Every expense or investment order is a single write: find → manual dedupe → create.
- `log_spending_transaction` writes one item at a time; it has no batch mode and no `dry_run`,
  and a retry with the same `source_ref` hit the same-day duplicate guard and errored instead of
  returning the original record.
- `create_investment_order` dedupes only on `source_ref`; without it there is no duplicate signal.
- `list_spending_transactions` is single-day only; `find_spending_transactions` returns one page
  and errors above `limit` matches instead of paginating, and passed the *next* local midnight as
  an inclusive upper bound (a row exactly at 00:00 the following day leaked into the range).
- No `dry_run` anywhere, so a caller cannot preview what would be created vs skipped.

## Goals
1. `log_spending_transactions_batch` — up to 50 items, per-item results with partial success by
   default, `atomic=true` for all-or-nothing.
2. `dry_run` on `create_investment_order` and `log_spending_transaction` (and the batch) — same
   structure as a real run minus created IDs: per-item `{index, status, match_reason, existing_id,
   errors[]}` plus top-level `counts`.
3. Dedupe: exact `source_ref` match returns the existing record ID; natural-key fallback
   (local day + amount + account + normalised description) for expenses without `source_ref`,
   multiplicity-aware.
4. Orders: exact `source_ref` dedupe; without it, a matching order yields a `possible_duplicate`
   warning and is **not** recorded (no silent skip, no silent create).
5. `list_/find_spending_transactions`: `from_date`/`to_date` (inclusive, workspace-local calendar
   day), opaque pagination cursor, `total_count`.

## Non-Goals
- A separate `idempotency_key` parameter (it would conflict with `source_ref`).
- Trade execution (orders stay record-keeping only).
- A unique index / migration for `source_ref` (see *Concurrency*).
- `simulate_sale` / `list_tax_lots` / `harvest_candidates` (kanban card `t_aa4d24ef`).

## Behaviour

### Per-item result vocabulary
`status` is one of: `created` (real run) / `would_create` (dry run), `duplicate`,
`possible_duplicate`, `invalid`, `not_created` (an atomic batch was rejected, so this valid item
was not written). `counts` = `{create, duplicate, possible_duplicate, invalid}` where `create`
covers `created` and `would_create`. Each item: `{index, status, match_reason, existing_id,
errors[], warnings[]}`; real-run created items add `entity_public_id`. A real run and a dry run of
the same input produce the same structure and counts; only created IDs differ.

### MCP tool `log_spending_transactions_batch` (new)
Params: `workspace_id`, `items` (1–50), `atomic=false`, `dry_run=false`, `skip_duplicates=false`.
Item fields: `amount`, `category_name`, `description?`, `account_name?`, `occurred_at?`, `tags?`,
`transaction_type?`, `source_ref?`, `allow_duplicate?`. Unknown fields make the item `invalid`
(a typo like `sourceref` must not silently drop idempotency). Item validation mirrors the single
tool (finite amount > 0 with ≤ 2 dp, no future date, category falls back to `other` with a warning,
account or workspace default account).

Classification, per item, in order:
1. `source_ref` present — exact match within (workspace, channel) → `duplicate`,
   `match_reason=source_ref`, `existing_id`. If the existing row's amount/type/account/description/
   day differ, the item is `invalid` (same contract as the single tool: a reused `source_ref` with a
   changed payload is an error). A repeated `source_ref` inside one batch → `duplicate` of the
   first (`duplicate_of_index`) when identical, `invalid` when different. Items with a `source_ref`
   skip the natural-key check.
2. No `source_ref` and not `allow_duplicate` — natural key = local day (user's timezone) + amount
   + account + normalised description (trimmed, lower-cased, whitespace-collapsed), within one
   transaction type. **Multiplicity-aware:** if the ledger holds `e` matching rows and the batch
   requests `k`, the first `min(e, k)` items (batch order) are matched one-to-one with existing
   rows and the remaining `k − e` are created. Ledger 1 × ₹50 chai + request 2 → 1 created, 1
   matched; empty ledger + request 2 → both created; replaying the batch afterwards matches both.
   A match is `duplicate` (`match_reason=natural_key`) with `skip_duplicates=true`, otherwise
   `possible_duplicate` (not written; set the item's `allow_duplicate=true` to record it anyway).
3. Everything else is created.

Write semantics:
- Default: per-item partial success. Each create runs in its own SAVEPOINT so one failing item
  cannot poison the others; a failing item becomes `invalid`.
- `atomic=true`: all-or-nothing. Any `invalid` or `possible_duplicate` item rejects the whole batch
  before anything is written (`status=error`, `persisted=false`, valid items shown as
  `not_created`). A failure while writing rolls the whole transaction back. A batch consisting only
  of duplicates succeeds with nothing created (so retries of an atomic batch are safe).
- `dry_run=true`: no writes at all (rows, tags, audit entries all unchanged — asserted in tests).
- More than 50 items, or an empty/non-list `items`: `status=error`, clear message, nothing written.

### MCP tool `log_spending_transaction` (changed)
New params `dry_run`, `skip_duplicates`.
- A retried `source_ref` with an identical payload returns the original record with
  `idempotent=true`, `match_reason=source_ref`, `existing_id` (previously the same-day guard
  returned `duplicate_detected`). A changed payload is still an error (spec-095 behaviour).
  A call carrying a `source_ref` does not also run the same-day guard (source_ref is authoritative).
- `skip_duplicates=true` (no `source_ref`): a natural-key match returns success with
  `duplicate=true`, `skipped=true`, `existing_id`, writing nothing. Without it the existing
  `duplicate_detected` refusal is unchanged.
- `dry_run=true`: runs the same validation, writes nothing, returns
  `{status:"success", dry_run:true, counts, items:[…]}` (one item). `possible_duplicate` is reported
  where a real run would refuse with `duplicate_detected`.

### MCP tool `create_investment_order` (changed)
New params `dry_run`, `allow_duplicate`.
- `source_ref` exact match → unchanged idempotent success, now also carrying `match_reason` and
  `existing_id`.
- No `source_ref`: an existing order on the same **user-local day** with the same account + symbol
  + side + quantity + price returns `status=possible_duplicate` (+ `existing_id`,
  `possible_duplicate_count`) and **records nothing**. Fees are deliberately not part of the match.
  `allow_duplicate=true` records a genuine repeat.
- `dry_run=true`: authorises with `mcp:write`, needs no `confirmed`, performs the duplicate checks
  and then runs the real `place_order` business validation (account, currency, cash, holdings)
  inside a SAVEPOINT that is always rolled back, so nothing persists. Returns the batch-style
  `counts`/`items` plus the computed amounts; validation failures are reported as an `invalid` item.

### MCP tools `list_spending_transactions` / `find_spending_transactions` (changed)
- New `from_date`, `to_date` (YYYY-MM-DD, both inclusive, user-local days), `cursor`. `day`
  (list) and `from_day`/`to_day` (find) remain. A missing `to_date` means the same day as
  `from_date`; no dates means today (list). `find` still requires at least one clue and still caps
  the span at 31 days; `from_date`/`to_date` now count as clues.
- The range is `[local 00:00 of from_date, local 00:00 of to_date + 1 day)` converted to UTC
  (DST-safe). The repository bound is inclusive, so the tools pass `end − 1µs`.
- Order is `occurred_at DESC, created_at DESC, id DESC` (total order → deterministic pages).
- Response adds `total_count` (all matches) and `next_cursor` (`null` on the last page). `find`
  keeps `total` as an alias and no longer errors when matches exceed `limit`.
- The cursor is opaque base64 JSON `{offset, filter-fingerprint}`; reusing it with different
  filters is rejected. It is offset-based, so a row inserted between page fetches can shift a page
  boundary by one (documented limitation; keyset pagination would need a repository change).

## Concurrency and idempotency under retries
There is no unique index on `(workspace_id, source_type, source_ref)`, and adding one needs a data
migration (existing duplicates must be resolved first), which this spec does **not** include. Instead
the write paths take `pg_advisory_xact_lock(<namespace>, workspace_id)` (new
`lock_idempotent_writes` on `TransactionRepository` / `InvestingOrderRepository`) before the
check-then-insert and hold it until the surrounding transaction commits or rolls back (the MCP and
voice runners commit after the tool returns). Concurrent identical requests in a workspace
serialise, and under READ COMMITTED the later one sees the earlier one's rows. Different workspaces
do not block each other. `get_by_source_ref` now returns the oldest match instead of raising
`MultipleResultsFound`, so a historical duplicate cannot wedge retries. Tests cover concurrent
source_ref, natural-key and order creation against real, independent connections.
**Recommended follow-up (not done):** a partial unique index on
`spending_transactions(workspace_id, source_type, source_ref) WHERE source_ref IS NOT NULL` and the
same for `investing_orders`, after a duplicate audit.

## Code map
- `app/capture/spending_batch.py` (new): pure helpers — description/amount normalisation, local-day
  windows, multiplicity planner, cursor codec.
- `app/capture/tools.py`: `log_spending_transactions_batch`, `dry_run`/`skip_duplicates` on
  `log_spending_transaction`, date range + cursor on list/find.
- `app/capture/agent.py`: dispatch entry for the batch tool.
- `app/spending/repository.py`: `get_by_source_refs`, `find_natural_key_candidates` (one query for
  the whole batch — no per-item lookups), `lock_idempotent_writes`.
- `app/investing/repository.py`: `find_possible_duplicate_orders`, `lock_idempotent_writes`.
- `app/mcp/server.py`: tool registration/parameters.
- No schema changes, no migration.

## Testing
`app/tests/capture/test_spending_batch_helpers.py` (planner/multiplicity, day windows incl. DST,
cursor), `test_spending_batch_idempotency.py` (batch semantics, dry-run shape and zero writes,
atomic rollback, partial success, 50-cap, single-tool dry_run/replay, list/find ranges, midnight
boundaries in IST and LA, pagination walk with tied timestamps, workspace scoping, query count),
`test_spending_idempotency_race.py` (concurrent duplicates on a cloned database),
`app/tests/investing/test_order_idempotency.py` (order dry_run with row-count assertions,
possible_duplicate, source_ref, local-day matching, authorization).

## Rollout
Single PR from `feat/batch-spend-idempotency-date-ranges`. Mark `Implemented` only after approval
and a green full suite in CI.
