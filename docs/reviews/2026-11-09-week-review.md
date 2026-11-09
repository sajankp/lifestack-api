# Week review - 2026-11-09 (specs 100-107, MCP surface)

Scope: the 34 commits / ~19.5k added lines merged between 2026-11-01 and 2026-11-09
(batch spend, LRS/FX, reconcile/audit/performance/health reads, sector look-through,
halal compliance, theme targets, tax what-if, portfolio report history).

Method: three independent read-only audits (spec-vs-code for 100/104/105, for
101/102/103/106/107, and an architecture/blast-radius study of `app/mcp/server.py`),
then every *high* finding was re-checked against the code before work started. Findings
marked **verified** were reproduced with a failing test first; **reported** ones come from
the audit and have not been independently reproduced yet.

## 1. Fixed in the hardening PR (all verified: red test first, then fix)

| # | Defect | Impact | Fix |
|---|---|---|---|
| 1 | `set_fx_rate` and `update_tax_profile` never committed | Writes reported as success were rolled back. Rule 115 rates and tax-profile edits made over MCP were lost; the spec-102 FX read tools therefore never saw them. | commit added; round-trip tests |
| 2 | `update_investing_account_status` never committed | Same: market scope / exchange changes silently lost. | commit added; read-back test |
| 3 | `get_performance(account_id=...)` for an account with no activity returned the **workspace-wide** XIRR labelled `scope=account` | Silently wrong numbers. | not-computable + warning; TWR flagged workspace-wide |
| 4 | `create_investment_order`: a reused `source_ref` with different details returned idempotent success | The caller believed a new order existed; nothing was recorded. | conflict error naming the differing fields; dry run reports `invalid` |
| 5 | Order dry run ignored `allow_duplicate` | Dry run predicted `possible_duplicate` where the real run records the order. | passed through |
| 6 | `get_allocation_vs_target` crashed on its documented default | Unhandled `AttributeError`. | `new_cash` defaults to `"0"` |
| 7 | `get_lookthrough_exposure` defaulted `group_by` to `theme` | Contradicted the spec (omitted = all groupings). | `GroupBy` enum, default all |
| 8 | `set_fx_rate` message printed the `date` class; stored raw rate text | Confusing output; non-canonical stored value. | uses the date string; canonical decimal |
| 9 | Report tools: dropped corrections were silent, null JSON for omitted fields, NaN/overflow headroom, `tax_buckets` outside the size cap, huge default list pages | See spec-107. | `created`/`content_matches`/`warning`, `{}`/`[]` defaults, validation, `include_payload=false` default |

Permanent guard: `app/tests/test_mcp_write_tools_commit.py` statically fails the build if any
`mcp:write` / `mcp:research` tool lacks a commit path. Run against the old code it flags
exactly items 1 and 2.

## 2. Reported by the audits, not yet fixed (ranked)

Correctness (reproduce first, then fix with TDD):
- **Shared fund snapshots are writable by any workspace** *(high, design - deliberately NOT
  fixed in the hardening PR).* `create_instrument` makes an instrument **global** whenever the
  live price source recognises its symbol, and `upsert_constituents` / `delete_snapshot` never
  check ownership, so any workspace can overwrite or delete a global fund's constituent
  snapshot and change every other workspace's look-through. A simple "global is read-only"
  guard was tried and removed: it blocks the normal flow for any new ETF that resolves a price
  (e.g. VTI), and forking on write alone would orphan the data because holdings keep pointing
  at the global instrument. Production today has 0 constituent rows on global instruments (all
  11 globals are stocks), so nothing is damaged. Proper fix: give snapshot rows a
  `workspace_id` (shared rows `NULL`, read-through with workspace rows shadowing) or fork the
  instrument **and repoint holdings/orders**, as `fork_for_workspace` does for companies. Needs
  a spec and a migration; schedule before onboarding other users.
- REST `lookthrough` response mixes two valuations (reporting currency picked differently;
  missing FX handled differently) - `performance_service.get_allocation_breakdown` vs
  `ExposureAnalyticsService.sector_allocation`. *(high)*
- Two parallel look-through engines (`sector_allocation`, `ThemeExposureService`) give
  different answers for the same portfolio (unmapped bucket label, missing price, default
  currency, sector normalisation, coverage of `OTHER-*` rows). *(medium)*
- `upsert_constituents`: O(rows x companies) company resolution; two rows resolving to one
  company raise an unhandled `IntegrityError`. *(medium)*
- `set_company_sector` confirmation preview resolves nothing (no before values, no
  not-found/ambiguous errors until after confirming). *(medium)*
- Forking a global company repoints only workspace-owned constituent rows, so a global
  ETF's look-through keeps the old sector. *(medium)*
- Spending: advisory lock only on `source_ref` / `skip_duplicates` paths; batch
  `same_payload` ignores category; single-tool `dry_run` skips amount validation. *(medium)*
- `return_metrics_service`: missing price silently falls back to avg cost; holdings with no
  orders are omitted from totals; FX conversion is N+1. *(medium)*
- TWR buckets transfers by UTC date. `simulate_sale` classifies long/short term by calendar
  day while the filing engine compares timestamps, so the same lot can be taxed differently.
  *(medium)*
- `set_fx_rate` read-modify-write of `fx_overrides` is not atomic. *(medium)*
- Low: `find_spending_transactions` 31-day cap allows 32 days; XIRR absolute tolerance;
  three timezone-default rules (UTC / IST / IST hard-coded); the 90-day staleness window is
  hard-coded in three services (should be a setting); `portfolio_reports` model and migration
  differ slightly in index naming / `String(128)` (autogenerate drift); stale spec text
  (migration parents, "pending approval").

## 3. Architecture

Problem: `app/mcp/server.py` is one 3.8k-line file whose `create_mcp_server()` closure holds
65 tools plus 2 resources (82% of the file). 161 `Repository` references mean tools build
repositories and run business logic inline, bypassing the routers -> services -> repositories
layering the rest of the app follows. Several tools are 100-200 lines.

Target (pure moves first, behaviour byte-identical):

```
app/mcp/server.py            thin: builds FastMCP, calls each tools.<domain>.register(mcp)
app/mcp/security.py          authorize_* / validate_page  (single patch seam for auth)
app/mcp/support/capture.py   run_capture_tool
app/mcp/support/services.py  service factories (one composition module)
app/mcp/tools/{todos,spending,health,finance,workspace,tax,compliance_themes,
               investing_research,investing_portfolio,investing_orders}.py
```

Rules enforced by a test: tool modules never from-import a patchable seam (call it through
its home module), never import `app.mcp.server` or each other. A golden catalog test
(`name`, description, input schema for every tool/resource) proves the split changes nothing.
Test blast radius measured: 12 test files, ~50 `monkeypatch` strings + 7 helper imports,
all mechanical.

Then, in separate PRs, move business logic out of tool bodies into services: order
idempotency/preview (`InvestingOrderService`), holdings valuation, `get_performance`
assembly, audit reads, health medication events, workspace reference data, tax-profile /
FX override writes. Split `app/investing/service.py` (2.4k lines, 8 classes) and
`app/investing/repository.py` (1.5k lines, 14 classes) into cohesive modules behind
re-exporting facades.

Out of scope for the move: behaviour changes. Each fix above lands first (this PR), so the
refactor diff is moves only.
