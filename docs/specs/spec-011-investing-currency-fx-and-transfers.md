# Feature Spec: Investing Currency Governance, FX Valuation, and Cross-Module Transfer Ledger
**Status:** Implemented (V1.1)
**Spec ID:** 011

## Implementation Notes (2026-05-24)
- Implemented backend resources under shared finance slice:
  - `currencies`, `workspace_currencies`, `accounts`
  - `workspace_finance_settings`, `fx_rates`, `capital_transfers`
- Added APIs:
  - `GET /v1/finance/settings`, `PATCH /v1/finance/settings`
  - `POST /v1/finance/fx-rates`, `GET /v1/finance/fx-rates`
  - `GET /v1/finance/transfers`, `GET /v1/finance/transfers/{public_id}`, `POST /v1/finance/transfers`
- Investing summary now supports valuation status semantics:
  - `single_currency_native`, `multi_currency_unconverted`, `conversion_required`, `converted_available`
- Workspace currency/account validation is enforced in investing + transfer flows.
- Integration coverage added for finance settings/FX/transfers and investing summary conversion paths.
- Follow-up slice **V1.2 (Spec 012: look-through exposure and overlap analytics)** is also implemented.

## 1. Overview
The current investing module supports holdings and cash balances, but leaves several domain-critical concerns underspecified:
- Currency values are free-form strings.
- Account semantics are ambiguous.
- Portfolio valuation across currencies is incomplete without persisted FX rates.
- There is no explicit ledger bridge between spending and investing for transfers and FX charges.

This spec defines a controlled V1.1 design for currency/account governance, historical FX storage, valuation strategy, and transfer events between spending and investing.

## 2. Goals
- Enforce strict currency/account option control in backend and frontend.
- Support workspace-level configuration of allowed currencies and owned accounts.
- Introduce deterministic, auditable FX valuation.
- Clarify account meaning and avoid implicit side effects between modules.
- Add a transfer ledger that captures fees and FX conversion metadata.
- Preserve modular boundaries (`app/application/` for orchestration, module services for domain logic).

## 3. Non-Goals (for this slice)
- Real-time tick-level market data.
- Intraday or live FX/market pricing.
- Broker API execution/trade automation.
- Full investing transaction ledger redesign (V2 concern).
- Tax reporting logic.
- Automatic spending budget mutation on transfer creation (no hidden side effects).
- Retrofitting every existing spending transaction with account attribution in the same slice.

## 4. Problem Statement
### 4.1 Currency drift risk
Unconstrained currency strings create data quality issues (`usd`, `Usd`, `dollar`, typos), making aggregation and conversion unreliable.

### 4.2 Account ambiguity
`account_name` currently acts as a label, but does not communicate account type/provider intent or transfer compatibility.

### 4.3 Valuation reproducibility gap
Using external FX rates on-the-fly makes historical valuation non-reproducible. We need persisted rates with timestamp and source.

### 4.4 Cross-module money flow blind spot
If users conceptually “move money” from spending to investing, we need explicit events capturing amount, rate, fee, and net results.

## 5. Proposed Scope
### 5.1 Currency reference + workspace enablement
Replace free-text currency with table-driven references:
- `currencies` (global reference):
  - `code` (PK, ISO-like code, e.g. `INR`, `USD`, `GBP`)
  - `name`
  - `symbol`
  - `minor_unit` (decimal places)
  - `is_active`
- `workspace_currencies` (workspace whitelist):
  - `workspace_id`
  - `currency_code`
  - unique `(workspace_id, currency_code)`

Rules:
- Investing/transfer payload currencies must be present in `currencies` and enabled in `workspace_currencies`.
- Frontend selectors are sourced from workspace-enabled currencies only.

### 5.2 Account model as workspace entity
Replace implicit account labels with first-class workspace-scoped accounts:
- `accounts` table:
  - `id` PK, `public_id`
  - `workspace_id`
  - `name` (user-visible label, unique per workspace)
  - `account_type` (`bank` | `brokerage` | `wallet`) for V1.1
  - `default_currency_code` (FK to `currencies.code`)
  - lifecycle fields (`is_active`, timestamps)

Rules:
- Holdings, cash balances, and transfers reference `account_id` (not raw account strings).
- `default_currency_code` is a UX/reporting default, not a hard constraint that prevents multi-currency balances or positions inside the account.
- `account_type` remains enum in V1.1; can be promoted to a table later if needed.

### 5.3 FX rates store
Add `fx_rates` table (workspace-agnostic, global reference data):
- `id` PK
- `base_currency_code` (FK to `currencies.code`)
- `quote_currency_code` (FK to `currencies.code`)
- `rate` numeric (high precision)
- `as_of` timestamp (provider timestamp)
- `fetched_at` timestamp (ingestion time)
- `source` string (provider identifier)
- unique constraint on `(base_currency_code, quote_currency_code, as_of, source)`

Scope boundary:
- V1.1 stores and serves day-level reference rates only.
- Live or intraday pricing is explicitly out of scope and would require a future major-version architecture review.

### 5.4 Valuation strategy
Define reporting currency as a configurable workspace finance setting.
- Summary returns:
  - native totals by currency
  - converted total in reporting currency when configured
  - conversion timestamp(s) used

Rules:
- Use persisted day-level rates by valuation date, not live lookups.
- If direct pair missing, allow one-hop triangulation via `USD` in V1.1.
- If no reporting currency is configured yet, return native totals only and mark converted valuation as unavailable rather than hard-coding `USD`.
- If conversion unavailable, surface partial valuation with explicit warning metadata.
- Direct stored pair always wins over triangulated derivation.
- Synthetic triangulated rates are used at read-time only and are not persisted as first-class `fx_rates` rows.

Reporting currency decision:
- Store reporting currency in a dedicated `workspace_finance_settings` resource/table rather than directly on `workspaces`.

### 5.5 Transfer ledger (spending -> investing)
Add `capital_transfers` table and service contract:
- `public_id`
- `workspace_id`
- `actor_id`
- `from_module` (`spending`)
- `to_module` (`investing`)
- `from_account_id`, `to_account_id` (FK to workspace `accounts`)
- `from_currency_code`, `to_currency_code` (FK to `currencies.code`)
- `gross_amount`
- `fx_rate_used` (nullable if same currency)
- `fx_fee_amount` (forex charge)
- `platform_fee_amount` (broker/wire fee)
- `tax_amount` (withholding/stamp/etc.)
- `net_amount_received`
- `occurred_at`
- `notes`

Audit logging requirement:
- create/update/delete on transfer entity must emit audit rows with fee/rate details.

Transfer behavior decision:
- Creating a transfer records an explicit finance event only in V1.1.
- It does not automatically mutate spending balances, investing balances, or cash ledgers in this slice.
- If automatic posting is added later, it must be implemented as an `app/application/` workflow once both sides share stable ledger semantics.

### 5.6 Shared ownership and module boundary
The new reference entities in this spec should not be owned by the `investing` module alone.

Recommended ownership:
- `currencies`, `workspace_currencies`, `accounts`, `fx_rates`, and `capital_transfers` live in a shared finance/reference slice (for example `app/finance/`).
- `investing` and `spending` may depend on those shared references.
- Cross-module effects triggered by transfers remain in `app/application/`, not inside spending or investing services directly.

Rationale:
- Transfers and account references are shared financial concepts, not investing-only concepts.
- Keeping them out of `investing` avoids hidden coupling and makes future spending-account support cleaner.

## 6. API Contract (V1.1)
### 6.1 Reference-driven validation
- Investing create/update endpoints reject unsupported currency/account references with RFC 7807 validation responses.
- Currency/account selectors are sourced from workspace-managed tables.

### 6.2 FX endpoints
- `GET /v1/finance/fx-rates?base=USD&quote=INR&as_of=...`
- Internal scheduler ingestion endpoint/service only (no public mutation endpoint in V1.1).

Provider decision:
- V1.1 uses a provider adapter with a day-level historical/reference-rate provider as the initial implementation.
- Valuation reads use persisted rates from the database only; they do not call external providers at request time.

### 6.3 Transfer endpoints
- `GET /v1/finance/transfers`
- `POST /v1/finance/transfers`
- `GET /v1/finance/transfers/{public_id}`

### 6.4 Reference management endpoints
- `GET /v1/finance/currencies`
- `GET /v1/finance/accounts`
- `POST /v1/finance/accounts`
- `PATCH /v1/finance/accounts/{public_id}`

## 7. Architecture Tradeoffs
### Option A: Free-text currency + live conversion only
Pros:
- fastest to ship
- minimal schema work

Cons:
- inconsistent currency data
- non-reproducible historical valuation
- harder debugging/auditing

Verdict: Reject.

### Option B: Strict enums only (currency + account type)
Pros:
- simplest validation logic
- fast to implement

Cons:
- requires deploy/migration for every new currency expansion
- does not support workspace-level currency governance
- account identity still too implicit for transfer/reconciliation use cases

Verdict: Acceptable short-term fallback, not preferred.

### Option B2: Reference tables + workspace-scoped account/currency governance
Pros:
- consistent data quality with FK guarantees
- workspace-level control of allowed currencies
- first-class account identity for transfers and reconciliation
- extensible without frequent code deploys

Cons:
- additional schema and join complexity
- needs account/currency management UX

Verdict: Recommended.

### Option B3: Put shared financial references inside the `investing` module
Pros:
- fewer new top-level modules at first
- simpler initial code navigation

Cons:
- account, FX, and transfer concepts are reused by spending too
- encourages cross-module leakage into investing-owned services
- makes future shared finance features harder to place cleanly

Verdict: Reject.

### Option C: Add transfer as implicit side-effect inside spending/investing CRUD
Pros:
- fewer new endpoints

Cons:
- hidden behavior
- violates module boundary clarity
- difficult fee attribution and audit reasoning

Verdict: Reject.

### Option D: Explicit transfer ledger as first-class entity
Pros:
- clear money-movement semantics
- explicit fee and FX metadata
- supports future accounting and reconciliation

Cons:
- additional UI/API complexity

Verdict: Recommended.

## 8. Oversight / Risk Checklist
- **Stale FX rates:** must expose rate freshness metadata and ingestion health.
- **Precision errors:** keep Decimal everywhere; serialize as strings over wire.
- **Missing cross pairs:** define deterministic fallback (triangulation or explicit partial status).
- **Backfill migration:** existing records need normalization/default mapping for currency/account_type.
- **Spending module gap:** transfer ledger must not assume that all spending transactions are already account-aware.
- **User expectation mismatch:** clarify that transfer ledger records intent/effects; it does not execute external bank/broker operations.
- **Regulatory nuances:** fee/tax fields are informational unless later compliance scope is added.

## 9. Data Migration Plan (high-level)
- Add shared finance/reference tables: `currencies`, `workspace_currencies`, `accounts`, `fx_rates`, and `capital_transfers`.
- Seed `currencies` with initial codes (`INR`, `USD`, `GBP`).
- Backfill `workspace_currencies` from currencies already used by each workspace.
- Backfill `accounts`:
  - create inferred accounts from legacy `account_name` values per workspace
  - assign default `account_type` policy (`brokerage` for holdings, `bank` for cash unless overridden)
- Migrate holdings/cash/transfers to `account_id` and currency FK fields.
- Do not force historical spending transaction backfill in this slice; document that as a separate follow-on spec if spending becomes account-aware.
- Reject/flag legacy invalid currency values during migration with manual review path.

## 10. Observability
- Counters:
  - FX rates fetched/succeeded/failed
  - transfers created
  - valuation partial-failure count
- Structured logs:
  - provider source, pair, as_of, latency, success/failure
- Trace spans:
  - fx_rate_fetch
  - valuation_convert
  - transfer_create

## 11. Security and Integrity
- No client-provided “computed net” accepted without server recomputation guard.
- All monetary fields validated for non-negative/positive constraints by context.
- Transfer mutations enforce workspace scope and actor membership.

## 12. Test Strategy
### Backend
- FK/whitelist validation tests for investing + transfer payloads.
- FX rate repository/service tests (lookup, fallback, freshness).
- Valuation summary tests for:
  - single-currency
  - multi-currency direct pair using day-level persisted rate
  - triangulation case via `USD`
  - missing-rate partial status
- Transfer tests covering fee/tax/net arithmetic validation.
- Workspace isolation tests for transfers.
- Audit tests for transfer mutations.
- Shared module boundary tests proving that spending/investing interact with accounts/transfers only through shared references and `app/application/` workflows where appropriate.

### Frontend
- Form validation and workspace-backed account/currency selector rendering.
- Error states for unsupported/missing rates.
- Summary rendering with native vs converted totals.
- Transfer create UX including fee and rate disclosure.

## 13. Acceptance Criteria
- Currency values are FK-constrained to `currencies` and workspace-enabled via `workspace_currencies`.
- Accounts are workspace-scoped entities referenced by ID in investing and transfer flows.
- Account type is enum-constrained in V1.1.
- Shared finance/reference entities are not owned by the `investing` module alone.
- FX rates are persisted with source and day-level timestamp metadata.
- Valuation endpoint returns deterministic converted totals using persisted day-level rates only.
- Account/currency management endpoints exist for frontend selector hydration and account lifecycle management.
- Explicit transfer ledger exists with gross/rate/fee/tax/net fields.
- Transfer mutations are workspace-scoped and audited.
- Transfer creation does not implicitly mutate balances in V1.1.
- RFC 7807 responses are used for validation and conflict scenarios.

## 14. Locked Decisions For V1.1
1. Reporting currency lives in a dedicated `workspace_finance_settings` resource/table.
2. FX valuation is day-level only; live or intraday rates are out of scope.
3. Direct FX pairs win; one-hop triangulation via `USD` is allowed when direct pair is absent.
4. Valuation reads from persisted rates only and never calls the provider at request time.
5. Transfer creation records a finance event only and does not automatically mutate balances in V1.1.
6. `account_type` remains an enum in V1.1.
