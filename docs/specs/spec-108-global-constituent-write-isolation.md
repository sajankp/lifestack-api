# Feature Spec: Global constituent snapshot write isolation
**Status:** Proposed
**Spec ID:** 108

## 1. Overview

Constituent snapshots (ETF/mutual-fund holdings composition) live on
`investing_instrument_constituents` and are keyed to an instrument.
Instruments are either workspace-owned
(`investing_instruments.workspace_id` set) or global
(`workspace_id IS NULL`, the shared security master that every workspace
reads).

Today any workspace can write or delete the constituent snapshot of a global
instrument: `InstrumentRepository.get_by_public_id` returns global rows for any
workspace_id, and `ConstituentService.upsert_constituents` /
`delete_snapshot` never check that the instrument belongs to the calling
workspace. One workspace can therefore overwrite or corrupt snapshot data
every other workspace reads. Production currently has 0 such rows, so this is
fix-before-it-bites, not a data repair.

## 2. Requirements

### 2.1 Read access is unchanged
Workspace members may read constituent snapshots of any instrument visible in
their workspace, including global (security master) instruments. All GET paths
are untouched.

### 2.2 Write access requires workspace ownership of the instrument
A constituent snapshot may be created, replaced or deleted only for an
instrument whose `investing_instruments.workspace_id` equals the calling
`workspace_id`. Global instruments (`workspace_id IS NULL`) and instruments
owned by another workspace are rejected.

### 2.3 Failure is loud and writes nothing
A write attempt against a global or foreign-workspace instrument fails before
any delete or insert with a clear authorization error — `ForbiddenError`
(HTTP 403, type `forbidden`) naming the instrument and the rule — and no
constituent row is created, deleted or modified.

### 2.4 Single enforcement point
The check lives in `ConstituentService` (`upsert_constituents` and
`delete_snapshot`), so it covers the REST POST route, the MCP
`write_investment_constituent_snapshot` tool and the MCP
`delete_investment_constituent_snapshot` tool, all of which call the same
service methods.

The CSV import path (`app/imports/investing_constituents_import.py`) is not
affected: it resolves instruments through a workspace-scoped
`instruments_map` (`Instrument.workspace_id == workspace_id`, spec-034), so
global instruments never enter a commit batch, and its pre-commit delete only
touches instrument_ids from that workspace-scoped preview.

## 3. Testing scenarios

### 3.1 Workspace B cannot overwrite a platform (global) snapshot
Setup: platform instrument I with `workspace_id IS NULL`; a constituent
snapshot exists for I (inserted directly, simulating platform curation).
Action: workspace B calls `upsert_constituents` (or the MCP write tool) on I.
Expected: `ForbiddenError`; snapshot rows unchanged (same count, same
weights).

### 3.2 Workspace B cannot delete a platform snapshot
Same setup; `delete_snapshot` (or the MCP delete tool) on I → `ForbiddenError`;
rows unchanged.

### 3.3 Workspace B cannot modify workspace A's snapshot
Setup: workspace A owns an ETF/MF instrument with a constituent snapshot.
Action: workspace B calls `upsert_constituents` / `delete_snapshot` on A's
instrument. Expected: `ForbiddenError`; snapshot unchanged. Workspace A can
still write its own snapshot (positive control).

### 3.4 Reads still work across the boundary
Workspace B can `get_constituents` on the global instrument and on workspace
A's instrument (read paths untouched).
