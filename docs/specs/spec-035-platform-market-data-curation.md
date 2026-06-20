# Feature Spec 035: Platform Market Data Curation

**Status:** Deferred (Backlog / Future Roadmap)
**Spec ID:** 035

---

## 1. Overview

Spec 034 covers workspace-facing constituent CSV import so users can make look-through analytics useful
when automated ETF/MF constituent providers are incomplete or unavailable.

This spec parks a later, broader capability: platform-level curation of shared market data. That
would include global constituent datasets, provider/imported instrument prices, licensed market-data
uploads, and corrections that can be reused across workspaces.

This is intentionally deferred. It introduces a platform-admin persona, global-vs-workspace ownership
rules, licensing provenance, rollback semantics, and data-quality review workflows. Those are useful
eventually, but they are overkill for the current product stage.

---

## 2. Relationship to Existing Specs

- **Spec 031:** implemented on-demand price refresh and manual holding price edits, currently
  workspace/holding-oriented.
- **Spec 032:** historically implemented automated constituent ingestion for ETF/MF instruments; the
  unreliable Yahoo runtime was retired on 2026-06-19.
- **Spec 033:** defers the hybrid/global instrument catalog with tenant overrides.
- **Spec 034:** active/proposed workspace-facing constituent CSV import.
- **Spec 035:** defers platform-admin/global market-data curation until the catalog and permission
  model are ready.

---

## 3. Future Personas

### 3.1 Platform Admin

A platform admin can curate shared market facts that may be reused across workspaces:

- global instrument metadata
- global constituent snapshots
- global provider/imported instrument prices
- source/provider/licensing metadata
- corrections and rollback

This is separate from workspace roles. A workspace owner/admin should not be able to mutate shared
global market data for every workspace.

### 3.2 Workspace Owner/Admin

A workspace owner/admin can still provide local data for their own workspace, such as the constituent
CSV import described in Spec 034 or manual price overrides. Workspace-local data should not become
global unless a platform-admin workflow explicitly promotes or imports it.

---

## 4. Future Data Ownership Model

Future implementation should keep these ownership boundaries:

- **Global market facts:** shared instruments, shared companies, provider prices, curated/licensed
  constituent snapshots.
- **Workspace market overrides:** user-entered/manual price overrides and workspace-local constituent
  snapshots.
- **Portfolio facts:** holdings, accounts, cost basis, quantities, snapshots, reporting currency, and
  FX assumptions.

### 4.1 Price Lookup Priority

When valuing a holding:

1. Workspace-local manual override for the instrument/date.
2. Global provider/imported instrument price on or before the date.
3. Last known applicable price.
4. Holding `avg_cost` fallback with explicit stale/missing-price metadata.

### 4.2 Constituent Lookup Priority

When computing look-through analytics:

1. Workspace-local constituent snapshot for the instrument/date.
2. Global constituent snapshot for the matching global instrument/date.
3. Automated provider snapshot.
4. Missing-data warning.

---

## 5. Future Schema Sketch

This is not an approved implementation contract.

```text
platform_market_data_import_batches
- id
- public_id
- uploaded_by_user_id
- scope: global | workspace
- workspace_id nullable
- data_type: constituents | prices | instruments
- source_name
- license_note
- file_name
- status: validating | committed | rejected | rolled_back
- created_at
```

```text
investing_instrument_prices
- id
- instrument_id
- price_date
- unit_price
- currency_code
- source
- provider_key
- import_batch_id nullable
- fetched_at
- created_at
- unique(instrument_id, price_date, source)
```

```text
workspace_instrument_price_overrides
- id
- workspace_id
- instrument_id
- price_date
- unit_price
- currency_code
- source
- import_batch_id nullable
- created_by_user_id
- created_at
- unique(workspace_id, instrument_id, price_date)
```

---

## 6. Deferred Acceptance Criteria

This spec should not be implemented until selected as an active roadmap slice. When reactivated:

- Add explicit platform-admin authorization separate from workspace roles.
- Add validation-preview-commit-rollback flows for global market-data imports.
- Store provenance and license notes for every platform-imported dataset.
- Ensure workspace-local overrides remain isolated and never mutate global rows.
- Ensure analytics and valuation clearly report whether data came from workspace override, global
  import, provider fetch, or fallback.
- Add integration tests proving workspace users cannot write global market data.
