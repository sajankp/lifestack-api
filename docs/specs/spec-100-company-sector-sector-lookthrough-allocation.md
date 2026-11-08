# Spec-100: Company Sectors and Look-Through Sector Allocation

**Status:** Draft - retroactive, pending Sajan approval (implementation written before approval; not claimed approved)
**Spec ID:** 100
**Depends on:** spec-012 (look-through exposure/overlap, constituent snapshots), spec-083 (company identity), spec-094 (MCP investment research tools)

## Problem

Companies in `investing_companies` already carry an optional `sector` (max 100 chars), but nothing in the MCP surface lets a research agent set it, and constituent snapshot rows cannot carry sector/industry provenance. As a result every direct stock and every ETF/MF line reads as "Unclassified" or "ETFs & Funds", and the personal agent computes sector allocation by hand.

## Goals

1. MCP tool `set_company_sector`: bulk, confirmed, workspace-scoped write that sets sector (and optional industry/country) on the company behind a holding symbol or on a company identified by ISIN/ticker+exchange. Returns before/after values.
2. `write_investment_constituent_snapshot` rows accept optional `sector`/`industry`; the write fills an empty company sector/industry and overwrites an existing one only with an explicit `overwrite_existing` flag.
3. Reads return sector: `get_investment_constituents` (per row) and `list_investment_holdings` (via the holding's company).
4. Look-through sector allocation (analytics + MCP read): ETF/MF holdings split across sectors by their latest constituent snapshot; `OTHER-<FUND>` placeholder holdings and unmapped companies land in "Other / not itemised" and are never rescaled; direct stocks use their own company's sector. Output: value and % per sector (summing to 100% with the Other bucket) plus coverage % per fund.
5. Exactly one constituent snapshot per instrument counts in look-through: newest by `as_of_date` then `fetched_at`; document and enforce the rule.

## Non-goals

- UI changes (web repo). The personal agent loads data via MCP once the tools exist.
- Derivative look-through, live ingestion, or a global sector vocabulary enforcement (free text; GICS names recommended).

## Design

### Sector vocabulary

Free text, max 100 characters (matches `investing_companies.sector`). GICS names are recommended: Information Technology, Health Care, Financials, Industrials, Energy, Consumer Staples, Consumer Discretionary, Materials, Utilities, Communication Services, Real Estate. Callers are expected to send these GICS names; the service does not validate against a whitelist.

### Model

`investing_companies` gains an `industry` column (nullable, max 100). `sector` already exists. No constituent-table changes: a constituent's sector is read through its company.

### set_company_sector (MCP, `mcp:research`, confirmed)

Inputs:
- `workspace_id` (int)
- `rows`: list of rows, each row is either
  - `{holding_public_id, sector, industry?, country_code?}` — the company behind a holding in this workspace, or
  - `{isin, sector, industry?, country_code?}` — company by ISIN (workspace or global), or
  - `{ticker, exchange, sector, industry?, country_code?}` — company via an instrument listed on that exchange (workspace or global).
  Exactly one identification key is required per row. `sector` is required; `industry`/`country_code` optional.
- `confirmed` (bool)

Behavior per row:
- Resolve the target company. Holding path: load workspace holding → instrument → company (error if no company linked). ISIN path: `company_repo.get_by_isin` scoped to workspace-or-global. Ticker+exchange path: instruments matching symbol+ticker exchange in workspace-or-global → their companies; zero matches is an error, more than one distinct company is an "ambiguous" error.
- If the company is global (`workspace_id IS NULL`), fork a workspace-scoped copy (same name/ticker/isn/country) and repoint the instrument (forking the instrument first if it is global, mirroring spec-099) so the write stays workspace-scoped.
- Record before values; set sector/industry/country_code when provided; save; record after values.
- Not confirmed → `needs_confirmation` preview listing before values and row count, no writes.
- Confirmed → returns per-row before/after plus skipped rows with reasons.

### Constituent snapshot sector propagation (mcp:research)

`InstrumentConstituentCreate` gains optional `sector`/`industry` (max 100). `InstrumentConstituentUpsert` gains `overwrite_existing: bool = False`.

In `upsert_constituents`, per row after resolving the company:
- if row.sector is not None and (company.sector is None or overwrite_existing): set company.sector.
- if row.industry is not None and (company.industry is None or overwrite_existing): set company.industry.
- save changed companies.

Reads: `InstrumentConstituentResponse` gains `sector`/`industry` read from the constituent's company; `get_investment_constituents` docstring updated. `list_investment_holdings` MCP items gain `company_sector`/`company_industry` via the holding's company.

### Duplicate snapshot rule (enforced)

`InstrumentConstituentRepository.get_latest_on_or_before` / `get_latest_on_or_before_many` return exactly one snapshot per instrument: the snapshot (as_of_date, source) of the row with the max `(as_of_date, fetched_at)` among rows with `as_of_date <= requested`. All rows of that snapshot are returned; rows from any other snapshot (same date different source, or older date) are not. This is the single source for look-through reads.

### Look-through sector allocation

New `ExposureAnalyticsService.sector_allocation(workspace_id, as_of, lookthrough=True)`:

Valuation mirrors `get_allocation_breakdown`: market value = quantity × latest price on/before `as_of` (avg-cost fallback), converted to workspace reporting currency (setting → single native currency → USD), holdings only (no cash).

Per open holding:
- symbol `^OTHER-.*` (case-insensitive): placeholder → whole value to "Other / not itemised"; not listed in fund coverage.
- stock: company sector if set, else "Other / not itemised" (unmapped company).
- etf/mutual_fund: load exactly one latest constituent snapshot (as above).
  - no snapshot → whole value to "Other / not itemised", fund coverage 0%.
  - snapshot rows: `itemised_weight = sum(weights)` capped at 1.0. Each row: row company sector if set → that sector bucket; else → "Other / not itemised". Remainder `1 - itemised_weight` → "Other / not itemised". Fund coverage = itemised_weight × 100.

Output: sectors [{sector, value, pct}] sorted desc by value (pct rounded 2 dp), total value, currency, every sector pct summing to 100 including "Other / not itemised"; fund_coverage per fund [{instrument_public_id, symbol, name, instrument_type, value, coverage_pct, snapshot_date, source, constituent_rows}]; warnings (missing price, missing snapshot, stale snapshot beyond window).

### REST

`GET /v1/investing/analytics/allocation?lookthrough=true|false` — when true, sectors come from the look-through computation; asset-class buckets unchanged.

## As-built decisions (review of the retroactive implementation)

- **Sector normalisation.** Writes strip/collapse whitespace and canonicalise a case-insensitive match of a GICS name or `Other / not itemised` (`health  care` -> `Health Care`); other free text keeps its casing. Allocation groups on a case/whitespace-insensitive key, so legacy rows written before this rule still share one bucket. Blank/whitespace-only sector on `set_company_sector` is rejected ("sector is required"); blank sector/industry on a constituent row means "not provided". Code: `app/investing/sectors.py`.
- **Forking.** `CompanyRepository.fork_for_workspace` never edits a global company. It reuses an existing workspace copy (same ISIN, else same name) so re-runs are idempotent, and re-points what the workspace owns: workspace instruments, a workspace shadow of each global instrument linked to the company (holdings in the workspace move onto the shadow, all accounts, not just the targeted one), and the workspace's own constituent rows. `get_by_isin` and company matching prefer the workspace copy over the global row. Constituent sector propagation uses the same fork, and only when a change would actually be made.
- **Transaction semantics of `set_company_sector`.** Each row runs in its own SAVEPOINT. Expected row failures (not found, ambiguous ticker, name clash) are returned in `errors`, their partial writes (e.g. a fork) are rolled back, and the other rows still commit. Any unexpected exception rolls back the whole call; nothing is committed and the tool returns `status=error`.
- **Single-snapshot reads.** `get_constituents` (REST and MCP) now also uses the single newest snapshot by `(as_of_date, fetched_at)` (tie-break: source) instead of mixing every source on the requested date.
- **Rounding.** Sector values and percentages are rounded with the largest-remainder method: values sum exactly to the total, percentages to exactly 100.00.
- **FX / warnings.** A holding whose FX rate is missing is excluded from the allocation and named in `warnings` (never counted as zero). A missing price falls back to average cost with a warning. Multi-currency with no reporting currency converts to USD and warns. All reads are batched (instruments, companies, constituents, prices): no per-holding queries.
- **REST.** `GET /v1/investing/analytics/allocation?lookthrough=true` additionally returns `lookthrough`, `fund_coverage` and `warnings`; asset-class buckets are unchanged.

## Risks

- Global company forked on first sector write in a workspace: two workspaces setting different sectors on the same global company diverge (intentional — workspace-scoped override, same precedent as spec-099 instrument forks).
- Constituent weights slightly over/under 1.0 due to provider rounding: remainder/residual handling keeps the total at exactly 100%.
- Staleness: look-through uses whatever latest snapshot exists; warnings flag staleness vs `staleness_window_days`.

## Acceptance criteria

- Bulk confirmed `set_company_sector` sets sectors for all holdings in one call; reading them back (`list_investment_holdings`, `get_investment_constituents`) shows the sector.
- Constituent write with a sector fills an empty company sector; a second write without `overwrite_existing` leaves it unchanged; with `overwrite_existing` it changes.
- Look-through allocation for a workspace returns sector percentages summing to 100% including "Other / not itemised"; an `OTHER-<FUND>` placeholder holding's full value lands in that bucket.
- Two snapshots for one instrument (same date, different source; or different dates): look-through uses only the newest by `(as_of_date, fetched_at)`.
- MCP tool docstrings document the new arguments and behavior.
