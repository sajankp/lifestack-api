# Feature Spec 034: Investing Constituent CSV Import

**Status:** Implemented
**Spec ID:** 034

---

## Implementation Notes (2026-06-14)

- Implemented `investing-constituents` as a CSV import module.
- Added template headers for `instrument_symbol,company_name,company_ticker,weight,as_of_date`.
- Added validation for ETF/MF instrument resolution, required company fields, date parsing, positive
  weights, and per-instrument/date weight totals.
- Added commit behavior that replaces existing `csv_import` constituent snapshots for the target
  instrument/date and writes new `InstrumentConstituent` rows.
- Added integration coverage for validation failures, successful commit, and snapshot overwrite.

## 1. Overview
Before this slice, constituents could only be populated automatically via the Yahoo Finance provider
job. If Yahoo was blocked/rate-limited, or if the user held custom/private pooled funds,
look-through constituent analytics could not be populated.

This spec added an **Investing Constituent CSV Import** module. This allows users to manually upload
look-through weight allocations for their ETF/MF instruments.

---

## 2. API & Data Model Changes

### 2.1 ImportModule Enum
Added a new option to `ImportModule` enum:
* `investing_constituents = "investing-constituents"`

### 2.2 CSV Schema & Headers
The CSV template headers are:
`instrument_symbol,company_name,company_ticker,weight,as_of_date`

Example row:
`UMMA,Apple Inc,AAPL,0.082,2026-06-14`

---

## 3. Validation Rules

During `validate_upload`:
1. **Instrument Verification:**
   * The `instrument_symbol` must resolve to an active `Instrument` of type `etf` or `mutual_fund` in the user's workspace.
   * If it is a stock, fail validation with an error.
2. **Field Requirements:**
   * `company_name` is required.
   * `weight` must be a positive decimal (`> 0`).
   * `as_of_date` must be a valid date in `YYYY-MM-DD` format.
3. **Weight Sum Constraint:**
   * For any single `instrument_symbol` on a specific `as_of_date`, the sum of all row weights must add up to approximately `1.0` (acceptable range: `0.99` to `1.01`).
   * Alternatively, weight input can be represented as percentages (e.g. `8.2` or `8.2%`) and scaled/renormalized automatically. For V1, we will expect weight as a decimal fraction (e.g., `0.082` for 8.2%) and validate that the sum is between `0.99` and `1.01`.

---

## 4. Commit Behavior

During `commit_import`:
1. Delete any existing `InstrumentConstituent` records for the target `instrument_id` on the specified `as_of_date` under the source `"csv_import"`.
2. For each row:
   * Resolve the `Company` in the current workspace by `name`. If not found, create a new workspace-scoped `Company` record.
   * Insert a new `InstrumentConstituent` record linking the instrument and company.

---

## 5. Acceptance Criteria
* The `/v1/imports` endpoint accepts `investing-constituents` module CSV files.
* Validation checks instrument presence and weight totals.
* Committing updates the look-through database state.
* UI has `Constituents` template download and import option.
