# Spec 022 - Workspace Currency and Display Governance

**Status:** Partially Implemented

Implementation note (2026-06-11): backend finance settings, currencies, FX rates, investing reporting currency, and user finance settings are implemented. Full status remains partial because this spec is cross-repo and includes frontend-wide currency-display cleanup.

## 1) Problem
Currency behavior is inconsistent across the UI:
- Spending surfaces still render hardcoded `$` in multiple places.
- Investing mostly uses data-driven currency but still falls back to `USD` in some UI paths.
- Users expect a workspace-level default display currency to apply consistently, while preserving investing's native/per-asset multi-currency behavior.

## 2) Goal
Define a single currency display contract across backend and frontend:
- Workspace-level display/reporting currency applies app-wide by default.
- Investing remains multi-currency native for positions, with optional reporting-currency conversion for aggregates.
- No page should silently hardcode `$` unless explicitly configured.

## 3) Scope
### In Scope (Phase 1)
- Standardize currency display rules for Spending, Dashboard, Imports summary cards, and Investing totals.
- Use existing finance settings endpoint (`/v1/finance/settings`) as source of workspace reporting/display currency.
- Replace hardcoded symbol rendering with formatter-driven currency rendering.
- Define fallback behavior when reporting currency is missing.

### Out of Scope (Phase 2+)
- Per-user currency overrides independent of workspace.
- Historical FX replay by point-in-time for every view.
- Locale profile settings beyond currency (date/number style presets).

## 4) Functional Requirements
### 4.1 Workspace Currency Source
- Currency display source of truth is workspace finance settings (`reporting_currency_code`).
- If unset:
  - Spending/Dashboard use first enabled workspace currency, else `USD`.
  - Investing keeps current valuation status behavior (`single_currency_native`, `multi_currency_unconverted`, etc.).

### 4.2 Spending and Dashboard
- All totals and amount fields must render via shared formatter (`formatCurrency`) with resolved workspace display currency.
- Budget inputs can stay numeric, but read views must show formatted currency string.
- Remove all hardcoded `$` markers in headers/cards/tables/modals.

### 4.3 Investing
- Row-level holdings/cash continue using native row currency.
- Aggregate cards:
  - use `summary.reporting_currency` when provided by backend;
  - else follow existing valuation status and show `N/A` for unconvertible multi-currency aggregates.
- Never force-convert native rows just for display.

### 4.4 Capture and Imports UX
- Any previewed/returned amount should include code-aware formatting, not a fixed symbol.
- Import validations continue enforcing valid currency code membership.

## 5) API/Data Contracts
- Reuse existing:
  - `GET /v1/finance/settings`
  - `PATCH /v1/finance/settings`
  - `GET /v1/finance/currencies`
- No new DB table required in Phase 1.
- Optional frontend helper:
  - `useWorkspaceCurrency()` hook built on top of finance settings + currencies query.

## 6) UX Requirements
- Currency code should be visible where ambiguity exists (for example multi-currency tables).
- For display-only cards, symbol format is acceptable if tied to resolved currency code.
- If reporting currency is unset and a view is ambiguous, show short helper text:
  - "Reporting currency not configured."

## 7) Security and Isolation
- Currency settings remain workspace-scoped.
- No cross-workspace leakage in cache keys; include workspace-context query segmentation as needed.

## 8) Acceptance Criteria
1. Spending page shows no hardcoded `$` strings in rendered amount UI.
2. Dashboard financial cards use resolved workspace display currency.
3. Investing rows remain native currency; aggregate cards follow reporting currency/valuation status contract.
4. Finance settings update reflects across app after cache invalidation/refetch.
5. Integration and component tests cover unset, set, and multi-currency states.

## 9) Test Plan
### Backend
- Existing finance settings tests extended for read/write behavior expectations.
- Investing summary tests retained for valuation status permutations.

### Frontend
- Unit/component tests:
  - spending totals formatting by configured workspace currency,
  - fallback when reporting currency missing,
  - investing aggregates vs row-native currency behavior.
- Regression checks for pages with previously hardcoded `$`.

## 10) Rollout
1. Implement formatter + workspace currency resolution hook.
2. Replace spending/dashboard hardcoded currency render paths.
3. Align investing aggregate fallback labels.
4. Ship with docs update in web+api README currency behavior notes.
