# Feature Spec 033: Hybrid Instrument Catalog with Tenant Overrides

**Status:** Implemented (migration `0032_hybrid_instrument_catalog.py`, 2026-06-24).
**Spec ID:** 033

---

## 1. Overview
Historically, workspace-scoped instruments caused redundant Yahoo Finance requests for common public
securities. That unreliable automated constituent runtime was retired on 2026-06-19; the hybrid catalog
model below was subsequently built.

This spec proposes transitioning to a **Hybrid Catalog Model** where:
* Public reference data is stored globally (`workspace_id IS NULL`).
* Custom or private holdings remain workspace-scoped (`workspace_id IS NOT NULL`).

---

## 2. Database Schema Changes

### 2.1 Schema Migration
* Alter `investing_instruments.workspace_id` and `investing_companies.workspace_id` to be nullable (`int | None`).
* Drop the existing compound unique constraints:
  * `uq_investing_instrument_workspace_symbol` on `(workspace_id, symbol)`
  * `uq_investing_company_workspace_name` on `(workspace_id, name)`
* Create partial unique indexes in Postgres to enforce integrity:
  * **Global Instrument Uniqueness:** `CREATE UNIQUE INDEX uq_global_instrument_symbol ON investing_instruments (symbol) WHERE workspace_id IS NULL;`
  * **Workspace Instrument Uniqueness:** `CREATE UNIQUE INDEX uq_workspace_instrument_symbol ON investing_instruments (workspace_id, symbol) WHERE workspace_id IS NOT NULL;`
  * **Global Company Uniqueness:** `CREATE UNIQUE INDEX uq_global_company_name ON investing_companies (name) WHERE workspace_id IS NULL;`
  * **Workspace Company Uniqueness:** `CREATE UNIQUE INDEX uq_workspace_company_name ON investing_companies (workspace_id, name) WHERE workspace_id IS NOT NULL;`

---

## 3. Logic & Service Layer Changes

### 3.1 Holding Creation
When a user adds a holding for a symbol (e.g. `SPY`):
1. Query `investing_instruments` for a global instrument (`workspace_id IS NULL`, `symbol = 'SPY'`).
2. If none exists, query for a workspace-scoped instrument (`workspace_id = current_workspace_id`, `symbol = 'SPY'`).
3. If neither exists:
   * Perform an on-demand check/fetch via Yahoo Finance.
   * If the ticker is public (Yahoo returns a valid response), create a **global instrument** (`workspace_id = None`).
   * If it is a custom/private ticker, create a **workspace-scoped instrument**.

### 3.2 Ingestion Job Optimization
* Currently, the constituent ingestion job queries all workspace-level instruments.
* Under the hybrid model, it will select:
  * All global instruments with `instrument_type` in `['etf', 'mutual_fund']`
  * Workspace-scoped custom instruments with `instrument_type` in `['etf', 'mutual_fund']`
* This limits constituent fetching to **exactly once per public ticker** regardless of how many workspaces hold it.

---

## 4. Acceptance Criteria
* Public ETFs/mutual funds (e.g. `SPY`, `UMMA`) and their constituents are created once globally.
* Automated constituent ingestion runs exactly once per public symbol across the entire database.
* Workspace-scoped custom assets (e.g. "Private Trust") remain isolated and invisible to other workspaces.
* All existing tests pass. New integration tests cover global vs workspace instrument lookup and ingestion.
