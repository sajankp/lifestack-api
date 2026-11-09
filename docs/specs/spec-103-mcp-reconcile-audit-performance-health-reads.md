# Spec-103: MCP Reconcile, Audit, Performance and Health Reads

**Status:** Draft (retroactive; pending Sajan approval) — the implementation exists on
`feat/mcp-audit-performance-health-reads` and was written before this spec. It is not
approved and must not be treated as approved until Sajan signs off.
**Scope:** authenticated MCP only; five new read-only tools; no REST, voice-agent or
schema (migration) changes
**Depends on:** spec-071 (XIRR / return metrics), spec-072/075 (FX resolution and display
rates), spec-093 (MCP workspace context), spec-094 (MCP investment tools)

## Problem

An agent connected over MCP can write to LifeStack (`create_investment_order`,
`log_weight`, `log_medication_event`, ...) but cannot check its own work or answer common
follow-up questions:

- Is LifeStack's view of my holdings the same as what INDmoney / Paasa shows? Which
  orders did I miss or record with the wrong fees?
- What changed recently, who changed it, and what were the before/after values?
- What is my return — money-weighted and time-weighted — in INR?
- What did I log for weight and medication? (`log_weight` / `log_medication_event` are
  write-only.)

## Goals

- Five read-only MCP tools, each authorized with `authorize_workspace(..., "mcp:read")`
  and every query scoped by `workspace_id`.
- Bounded inputs and outputs: capped page size, capped external list, deterministic
  ordering with a tiebreaker.
- Money is `Decimal` internally and serialised as strings.
- Failure of a computation (no cash flows, no FX rate, non-convergence) is reported as
  an explicit status or warning, never an exception and never a silent zero.

## Tools

### `reconcile_holdings(workspace_id, external_list, account_id?, quantity_tolerance?, cost_tolerance_pct?)`

Diffs an external holdings list against LifeStack holdings with a non-zero quantity.

- `external_list`: 1–500 entries, each an object with `symbol` and/or `isin`, `quantity`
  (required, finite, not negative), optional `avg_cost`, `currency`, `account_id`
  (LifeStack account UUID), `source`, `external_ref`. Malformed input returns
  `{"status": "error", "message": "row N: ..."}`.
- Normalisation: upper-case; exchange prefix (`NSE:`) and suffix (`.NS`, `.BO`, `-EQ`, ...)
  removed; ISIN matched ahead of symbol (ISIN from the instrument, else its company).
  Rows for the same ISIN-or-symbol and account are merged (quantities add, average cost
  is quantity-weighted). With no external `account_id`, all LifeStack holdings of that
  security are aggregated; with one, only that account's holding is compared.
- Tolerance: `quantity_tolerance` is an absolute share count (default `0.0001`, absorbs
  float noise); `cost_tolerance_pct` is a relative average-cost difference (default
  `0.5`, range 0–100). Cost is not compared when currencies differ.
- Buckets (one per item): `matched`, `quantity_drift`, `cost_basis_drift`,
  `missing_in_lifestack`, `missing_at_depository`. Each non-matched item carries a
  `suggestion` with an `action` code (`record_buy_orders`, `record_sell_orders`,
  `review_order_fees`, `verify_or_remove_stale_holding`), the quantity involved and a
  message; `cost_basis_drift` includes `cost_delta_pct` and the implied total cost
  difference so a mis-recorded brokerage/tax/fee can be located.
- `account_id` limits the LifeStack side to one account. Without it, a warning is
  returned when several accounts hold positions, because `missing_at_depository` is
  only meaningful when the external list covers that account.
- Items are ordered by bucket, then symbol, then identifiers (deterministic). Holdings,
  instruments, companies and accounts are loaded with a fixed number of bulk queries,
  independent of the number of holdings.

### `list_recent_changes(workspace_id, limit=25, offset=0, module?, action?, entity_type?, entity_public_id?, since?, until?)`

Paginated read of the append-only `audit_logs` table, newest first (`timestamp DESC,
id DESC`). `limit` 1–100. `since`/`until` are ISO-8601 (naive = UTC). Each item has
`public_id`, `timestamp`, `module`, `action`, `entity_type`, `entity_public_id`,
`actor_public_id`, `actor_name` (username; resolved with one bulk query) and `details`
(`before`, `after`, `changed_fields`, `request_id`).

Redaction: values are redacted when written (`AuditLogger`) and again on read, so rows
written by older code or by other paths are covered. Key matching is exact for the
existing `SENSITIVE_KEYS` set plus case-insensitive substring matching for
`password`, `passwd`, `secret`, `token`, `api_key`, `apikey`, `private_key`, `hash`,
`authorization`, `credential`, `card_number`, `cvv`. This is deliberately conservative: a
key such as `tokenizer_note` is also hidden. Free-text values are not scanned.

### `get_performance(workspace_id, account_id?, period="all", reporting_currency="INR")`

- `period`: `all`, `1m`, `3m`, `6m`, `1y`, `3y`, `5y`, `ytd` (calendar months).
- XIRR (money-weighted) overall, per account and per holding, **since inception** from
  `ReturnMetricsService` (orders, dividends, and the terminal market value). Reported as
  a fraction (`xirr`) and `xirr_pct`. `xirr_status` is `ok`, `not_computable` (no cash
  flows, a single or all-same-sign flow, a sub-day span or non-convergence — the solver
  never raises) or, for the overall block, `conversion_required` (a multi-currency
  workspace without a reporting currency or historical rates; this is the existing
  spec-071 INV-1 behaviour).
- Time-weighted return over `period`: Modified-Dietz sub-period returns between
  consecutive portfolio snapshots, chain-linked, with capital transfers into/out of the
  investing module neutralised (converted with the later snapshot's stored FX rates).
  The result has `status` (`ok`, `insufficient_snapshots`, `mixed_snapshot_currency`,
  `missing_fx_for_flows`, `no_computable_periods`), `twr_pct` (null unless `ok`),
  `periods`, `periods_skipped`, and `warnings`. A sub-period with a non-positive opening
  value is skipped and counted.
- Amounts (`invested`, `market_value`, `realized`, `unrealized`; TWR start/end value and
  net flows) are shown natively and under `converted` in `reporting_currency` at the
  latest FX rate (spec-075 display cut-off, USD cross-routing). Overall converted amounts
  are the sum of per-account converted amounts. A currency with no rate gives `null`
  converted values, `valuation_status: "conversion_required"` and a warning naming the
  currency; XIRR and native values are unaffected.
- With `account_id`, accounts, holdings and the `overall` block are restricted to that
  account (`overall.scope = "account"`).

### `get_weight_history(workspace_id, start?, end?, limit=25, offset=0)`

Read counterpart of `log_weight`: `measured_at DESC, id DESC`, `weight_kg` as a string,
ISO-8601 bounds (naive = UTC), `limit` 1–100.

### `list_medication_events(workspace_id, medication_id?, status?, limit=25, offset=0)`

Read counterpart of `log_medication_event`: `scheduled_for DESC, id DESC`, filter by a
medication UUID in the workspace and/or `status` (`taken`, `skipped`), `limit` 1–100.
Medication names are resolved with one bulk query. `log_medication_event` and
`log_weight` already exist and are unchanged.

## Service and repository changes

- `ReturnMetricsService.get_performance_metrics` / `get_position_metrics`: per-position
  metrics; `get_return_metrics` is refactored onto the same helpers (behaviour
  unchanged) so a tool call builds positions once.
- `PerformanceService.get_time_weighted_return` (new optional `transfer_repo`).
- `CapitalTransferRepository.list_investing_flows_in_period`.
- `MedicationEventRepository.list_by_workspace`, `MedicationRepository.get_by_ids`;
  `WeightEntryRepository.get_range` gains an `id` tiebreaker.
- `AuditLogRepository.list_by_workspace`, `UserRepository.get_summaries_by_ids`.
- New pure modules `app/investing/reconciliation.py` and
  `app/investing/performance_report.py`.
- `redact_details` extended with substring matching (also applies to new audit writes).

## Non-goals and known limitations

- XIRR is since-inception; `period` windows only the time-weighted return. A windowed
  XIRR needs an opening valuation per position (quantity and price at the window start,
  corrected for splits/bonuses) and is not built.
- Time-weighted return depends on stored portfolio snapshots: sparse snapshots give a
  coarse series, and snapshots in different reporting currencies are not linked.
  Investing-to-investing transfer fees are treated as flows, not as return drag.
- Holdings without any order (for example imported holdings) have no cash flows and so
  no per-holding XIRR; this is existing `ReturnMetricsService` behaviour.
- `reconcile_holdings` never writes; acting on a suggestion is a separate, confirmed
  `create_investment_order` / `update_investment_order` call. It assumes the external
  list is complete for the account scope it is compared against. Splits and bonuses
  surface as `quantity_drift`.
- Audit redaction is key-based; secrets placed in free-text values are not detected.

## Testing

- Unit: normalisation, parsing and every bucket, tolerance edges, merging, ISIN
  matching, size cap (`test_reconciliation.py`); period windows and conversion/null
  rules (`test_performance_report.py`); time-weighted return incl. contributions,
  withdrawals, mid-period weighting, FX, empty/zero cases
  (`test_time_weighted_return.py`); redaction of compound key names
  (`test_audit_logging.py`).
- DB-backed through the real tool functions (`test_mcp_reads_db.py`): workspace
  isolation, pagination completeness with identical timestamps, filters, actor names,
  stored-secret redaction on read, constant statement count for reconcile, XIRR values,
  INR conversion, missing-FX warning, empty workspace.
- Catalog and authorization (`test_mcp_tools_suite.py`): tool names and count, `mcp:read`
  scope per tool, page-size cap, validation before authorization.
