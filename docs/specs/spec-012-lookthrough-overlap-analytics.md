# Feature Spec: Look-Through Exposure and Overlap Analytics for ETF/Mutual Fund Portfolios
**Status:** Implemented (V1.2)
**Spec ID:** 012

## Implementation Notes (2026-05-24)
- Implemented as **V1.2**, on top of completed **V1.1 (Spec 011)** finance/currency/FX foundations.
- Implemented day-level look-through data foundation:
  - `investing_companies`
  - `investing_instruments`
  - `investing_instrument_constituents`
  - `investing_holdings.instrument_id` linkage
- Added APIs:
  - `GET /v1/investing/instruments`
  - `POST /v1/investing/instruments`
  - `GET /v1/investing/instruments/{instrument_id}/constituents?as_of=YYYY-MM-DD`
  - `POST /v1/investing/instruments/{instrument_id}/constituents`
  - `GET /v1/investing/analytics/exposure?as_of=YYYY-MM-DD`
  - `GET /v1/investing/analytics/overlap?as_of=YYYY-MM-DD`
- Implemented fallback strategy for constituent snapshots:
  - use latest snapshot on or before requested date
  - include warning metadata and `analysis_status=partial` when coverage is incomplete
- Integration coverage added for mixed direct + pooled holdings, look-through exposure, and overlap metrics.
- Frontend delivery is completed in the paired web repository:
  - investing look-through analytics UI tab
  - unit tests and Playwright E2E coverage

## Follow-Up Implementation Notes (2026-06-13)
- Spec 032 implemented the ingestion side of this architecture:
  - holding creation and investing-holdings CSV import now support `instrument_type`
  - instruments can be corrected with `PATCH /v1/investing/instruments/{instrument_id}`
  - `constituent_ingestion_job` fetches and normalizes ETF/MF top holdings into day-level snapshots
  - the analytics UI includes an instrument correction panel for existing holdings

## Constituent Source Update (2026-06-19)

- The unreliable Yahoo automated-ingestion path from Spec 032 has been retired.
- Constituent snapshots remain supported through the CSV import workflow in Spec 034.

## 1. Overview
Users often hold a mix of direct stocks and pooled vehicles (ETFs, mutual funds). Portfolio totals alone hide concentration and duplicate exposure created by overlapping fund constituents.

This spec introduces a look-through analytics layer to compute:
- direct exposure (what the user directly owns)
- decomposed exposure via ETF/MF constituents
- overlap and concentration metrics across both layers

The design is day-level (snapshot-based), consistent with current valuation scope.

## 2. Goals
- Represent pooled instruments and their underlying company constituents.
- Compute deterministic, reproducible day-level look-through exposure.
- Surface overlap risk (duplicate company exposure across multiple funds/instruments).
- Preserve existing investing valuation semantics and workspace isolation.
- Keep ingestion/provider concerns decoupled from runtime portfolio reads.

## 3. Non-Goals (for this slice)
- Live/intraday holdings refresh.
- Trade execution, broker sync, or order management.
- Tax-lot decomposition.
- Derivative look-through (options, swaps, futures).
- Real-time NAV/price engine redesign.

## 4. Problem Statement
### 4.1 Hidden concentration risk
A portfolio can appear diversified by instrument count but be concentrated in a few companies after look-through decomposition.

### 4.2 Overlap blindness
Users can hold multiple funds that repeatedly allocate to the same large-cap constituents without visibility.

### 4.3 Reproducibility gap
If constituent composition is fetched ad hoc at request time, historical analysis becomes non-reproducible.

## 5. Proposed Scope
### 5.1 Instrument metadata extension
Introduce first-class instrument classification and canonical identity:
- `instruments`
  - `id`, `public_id`
  - `workspace_id` (nullable for global catalog mode; V1 can keep workspace-scoped)
  - `symbol`
  - `name`
  - `instrument_type` enum: `stock | etf | mutual_fund`
  - optional identifiers: `isin`, `exchange`, `provider_key`
  - lifecycle fields

### 5.2 Position model alignment
Keep user-owned quantities in `investing_holdings` (or a future generalized `positions` table), but require linkage to `instrument_id`.
- `holding` represents what user owns.
- decomposition is derived from current/historical constituent snapshots.

### 5.3 Constituent snapshots
Add historical constituent composition store:
- `instrument_constituents`
  - `id`
  - `instrument_id` (ETF/MF parent)
  - `constituent_company_id`
  - `weight` (0..1 decimal)
  - `as_of_date` (day-level)
  - `source`
  - `fetched_at`
  - unique `(instrument_id, constituent_company_id, as_of_date, source)`

Weight policy:
- prefer normalized weights summing to ~1.0
- allow tolerance window for provider rounding
- mark snapshot quality status if sum falls outside tolerance

### 5.4 Company reference
Add canonical company entity:
- `companies`
  - `id`, `public_id`
  - `name`
  - optional `ticker`, `isin`
  - optional `sector`, `country_code`

Purpose:
- collapse equivalent constituent identities across different providers/funds.
- enable overlap aggregation by a stable company key.

### 5.5 Analytics outputs
For a portfolio valuation date `D`:
- `direct_exposure_by_company`
  - only directly-held stocks mapped to company
- `lookthrough_exposure_by_company`
  - direct exposure + decomposed ETF/MF exposure
- `overlap_summary`
  - top overlapped companies
  - concentration metrics (top 5/top 10 %)
  - duplicate exposure index

Base formula:
- For each pooled holding `h` in instrument `F`:
  - `holding_value = quantity_h * price_h_or_avg_cost_proxy`
  - For each constituent `c` in snapshot of `F` at date `D`:
    - `exposure_value(c) += holding_value * weight(F->c,D)`

### 5.6 Date and fallback rules
- Primary: exact snapshot on requested date.
- Fallback: latest snapshot on or before date within configured staleness window.
- If no acceptable snapshot exists:
  - exclude pooled decomposition for that holding
  - return warning metadata with affected instruments

### 5.7 Ownership and module boundary
Recommended ownership under shared finance/investing analytics slice:
- data entities under `app/finance/` or `app/investing/` with clear interfaces
- orchestration and multi-step calculations in `app/application/` service

Rationale:
- decomposition combines holdings, price context, and constituent history
- avoids embedding cross-cutting orchestration directly in CRUD services

## 6. API Contract (V1)
### 6.1 Instrument and constituent management
- `GET /v1/investing/instruments`
- `POST /v1/investing/instruments`
- `GET /v1/investing/instruments/{instrument_id}/constituents?as_of=YYYY-MM-DD`
- `POST /v1/investing/instruments/{instrument_id}/constituents` (internal/admin ingestion path)

### 6.2 Overlap analytics endpoints
- `GET /v1/investing/analytics/exposure?as_of=YYYY-MM-DD`
  - returns direct + look-through exposure maps and warnings
- `GET /v1/investing/analytics/overlap?as_of=YYYY-MM-DD`
  - returns ranked overlaps and concentration metrics

### 6.3 Response semantics
- all numeric values serialized as strings
- include `as_of_date`, `snapshot_coverage`, `staleness_days`, and warning list
- if partial data, return HTTP 200 with explicit `analysis_status=partial`

## 7. Architecture Tradeoffs
### Option A: Store only aggregated overlap results
Pros:
- smaller storage
- faster reads

Cons:
- cannot recompute with updated rules
- weak auditability/debuggability

Verdict: Reject.

### Option B: Fetch constituents live at request time
Pros:
- always freshest

Cons:
- non-deterministic historical outputs
- external dependency in user request path
- poor latency/reliability

Verdict: Reject.

### Option C: Persist day-level constituent snapshots and compute on read
Pros:
- reproducible historical analysis
- clear fallback behavior
- aligns with day-level valuation scope

Cons:
- ingestion pipeline required
- more schema complexity

Verdict: Recommended.

### Option D: Hybrid (persist snapshots + optional materialized aggregates)
Pros:
- scalable for larger portfolios
- preserves recomputability

Cons:
- added invalidation complexity

Verdict: Future optimization after V1 correctness.

## 8. Risks and Mitigations
- **Identifier mapping drift:** same company may appear under different symbols.
  - Mitigation: canonical `companies` table + mapping rules.
- **Stale constituent data:** outdated overlap can mislead.
  - Mitigation: staleness metadata and warnings.
- **Weight quality issues:** provider weights may not sum to 100%.
  - Mitigation: tolerance checks + snapshot quality flag.
- **Performance:** decomposition can be heavy for large universes.
  - Mitigation: batched SQL aggregation and optional cached/materialized layer later.
- **User trust:** partial analysis without clear messaging can confuse.
  - Mitigation: explicit `analysis_status` and per-instrument warning payload.

## 9. Implementation Plan (Phased)
### Phase 1: Data foundation
- Add tables: `instruments`, `companies`, `instrument_constituents`.
- Link holdings to `instrument_id` while preserving backward compatibility.
- Add migration/backfill for existing holdings (symbol-based inferred instruments).

### Phase 2: Analytics service
- Implement exposure decomposition service in `app/application/`.
- Add read endpoints for exposure + overlap.
- Add deterministic fallback rules for snapshot date selection.

### Phase 3: Ingestion and quality
- Add internal ingestion interface for constituent snapshots.
- Add snapshot quality validation and staleness metadata.
- Add scheduled day-level ingestion hooks (provider adapter abstraction).

### Phase 4: UX and alerts (future)
- Frontend overlap dashboard.
- concentration/duplicate exposure alert thresholds.

## 10. Open Questions
1. Should `instruments` be global catalog entries or workspace-scoped in V1?
2. Which valuation proxy should V1 use for decomposition when day market price is unavailable: `avg_cost`, last known close, or mixed policy?
3. What maximum staleness window is acceptable before marking pooled decomposition unavailable (e.g., 7 vs 30 days)?
4. Should we support manual constituent CSV upload in V1 as a fallback to API ingestion?

## 11. Acceptance Criteria
- User can hold direct stocks and ETF/MF instruments in same workspace.
- System can return day-level look-through exposure by company with reproducible inputs.
- Overlap endpoint highlights top duplicate company exposures and concentration metrics.
- Partial-data scenarios are explicitly marked with warning metadata.
- Full path has integration and e2e coverage for direct + pooled + mixed portfolios.
