# Spec-110: Health measurements — one store for any result over time (weight becomes a metric)

**Created:** 2026-11-16
**Status:** Draft — pending approval
**Scope:** `lifestack-api` (new `app/health/measurements/` package, alembic migration, MCP tools, weight migration, exports/briefing/summary integration) and `lifestack-web` (read-only Records section). E2E after both merge.
**Depends on:** spec-069 (health module — this spec generalises its `weight_entries` table; the source-triplet, audit and trust-boundary rules carry over), spec-103 (MCP health reads), the MCP batch-write pattern (`log_spending_transactions_batch`), the golden MCP catalog test.

---

## Problem

`weight_entries` stores one thing: a decimal in kilograms. Everything else the owner wants to
track over time — blood work, other lab results, vitals, prescriptions, anything a clinic hands
over — has nowhere to live, and each new metric as its own table would repeat the same
model/repository/service/router/MCP/export/briefing plumbing (roughly 1,400 lines for weight and
medications alone).

Spec-069 already named labs and vitals as "later Health Memory slices, same table patterns".
This spec does that once, generically, instead of per metric.

## Goals

- Store **any** result over time: numeric (weight, HbA1c, LDL, blood pressure), thresholded
  (`<0.5`, `>100`), and text (`Negative`, a prescription line).
- A **free note** at two levels: on every measurement, and on the **record** a bulk write
  creates, so provenance ("extracted from Metropolis PDF, 2026-11-10") travels with the data.
- **MCP-only ingestion** (owner decision). No web entry form for the generic store.
- **Weight becomes just another metric** (owner decision): same table, same MCP/REST surface,
  existing weight endpoints, briefing line, weekly-summary field, export and capture tool keep
  working.
- Units are never silently mixed: a series is comparable across labs or it is rejected.
- Bulk writes are idempotent, previewable (`dry_run`) and reversible (delete the record).

## Non-goals

- Web entry/edit forms for generic results (MCP only). The web gets a read-only view.
- Diagnosis, interpretation, targets or advice (spec-069 trust boundary). The only computed
  outputs are unit conversion, counts, deltas, min/max, and `in_range` **against the range
  printed on the report itself**.
- File storage. A record may cite its source in `source_ref`/`note` (filename, URL, lab
  reference number); the PDF itself is not stored.
- PDF/OCR extraction in the app. An MCP client reads the document and writes the rows
  (roadmap Track 4 stays separate).
- Creating medications or reminders from a prescription (a prescription is stored as a record;
  `medications` and its schedule stay as spec-069 defined them).
- Reference-range databases, age/sex-adjusted ranges, LOINC mapping, or cross-metric analytics.
- Device or health-app sync.
- Editing a measurement in place. Corrections are delete + re-log, as spec-069 decided for
  weight; a whole mis-extracted report is fixed by deleting its record and re-sending.

## Solution

### A. Data model (new package `app/health/measurements/`, same layering as `app/health/`)

Three workspace-scoped tables. Public ids are UUIDs, internal ids never leave the API.

**`health_metrics`** — the catalog of things that can be measured.

| column | notes |
|---|---|
| `id`, `public_id`, `workspace_id` | |
| `code` | slug `^[a-z][a-z0-9_]{1,63}$`, unique per workspace (`body_weight`, `hba1c`, `ldl_cholesterol`) |
| `name` | display name, ≤120 |
| `category` | free slug ≤32 (`body`, `blood`, `lipid`, `thyroid`, `vitals`, `prescription`, `other`) — grouping only, no behaviour hangs off it |
| `value_type` | `numeric` \| `text` |
| `canonical_unit` | nullable, ≤32; required for `numeric` unless the metric is unitless (pH, ratios) |
| `unit_conversions` | JSON `{ "<unit>": {"factor": n, "offset": n} }`, `canonical = value × factor + offset`. Offset covers affine pairs (HbA1c % ↔ mmol/mol) |
| `aliases` | JSON list of strings (`["SGPT", "ALT (SGPT)", "Alanine transaminase"]`) |
| `min_value`, `max_value` | optional plausibility bounds, service-enforced (not a DB check) |
| timestamps | |

Constraints: `UNIQUE (workspace_id, code)`; alias and name uniqueness is checked in the service
on normalised text (lowercase, collapsed whitespace/punctuation) across all metrics in the
workspace, so one alias can never resolve to two metrics.

`body_weight` is the one built-in: `numeric`, `canonical_unit = "kg"`, `category = "body"`,
`min_value = 0.01`. It is created lazily per workspace by `ensure_builtin_metrics` on first
health access (and by the migration script for existing workspaces), never by the migration
itself.

**`health_records`** — the "document or event" a set of results came from. Optional for a single
measurement, always created by a bulk write.

| column | notes |
|---|---|
| `id`, `public_id`, `workspace_id`, `user_id` | |
| `kind` | free slug ≤32: `lab_report`, `prescription`, `imaging`, `visit`, `import`, `other` |
| `title` | `TEXT`, API limit 160 (`"Lipid + thyroid panel"`, `"Dr Rao — 2026-11-10"`) |
| `recorded_on` | date — collection date for labs, issue date for prescriptions |
| `provider` | nullable ≤120 (lab, clinic or doctor) |
| `note` | nullable `TEXT`, API limit 2000 — **the free source/provenance note** |
| source triplet | `source_type` (`mcp` for bulk writes), `source_ref` (nullable, ≤255), `source_import_id` |
| timestamps | |

Constraint: partial unique index `(workspace_id, source_ref) WHERE source_ref IS NOT NULL`. A
report re-sent with the same `source_ref` is the same record.

**`health_measurements`** — one row per result.

| column | notes |
|---|---|
| `id`, `public_id`, `workspace_id`, `user_id` | |
| `metric_id` | FK → `health_metrics` (RESTRICT: a metric with data cannot be deleted) |
| `record_id` | nullable FK → `health_records`, **ON DELETE CASCADE** |
| `measured_on` | date, not null — the calendar day (workspace timezone) |
| `measured_at` | timestamptz, nullable — set only when a real time of day exists (weight, BP). Never a faked midnight |
| `value_num` | `Numeric(18,6)`, nullable |
| `comparator` | nullable: `<`, `<=`, `>`, `>=` — only valid with `value_num` |
| `value_text` | nullable `TEXT`, API limit 500 |
| `unit` | nullable ≤32, **as reported** |
| `ref_low`, `ref_high` | `Numeric(18,6)`, nullable, in the row's own `unit` — ranges belong to the lab, age and sex, not to the metric |
| `ref_text` | nullable `TEXT`, API limit 120, for ranges that are not a pair (`"Desirable: <200"`) |
| `note` | nullable `TEXT`, API limit 500 — free per-result note |
| source triplet | as above; `source_ref` has a partial unique index `(workspace_id, source_ref)` |
| timestamps | |

Constraints: `CHECK (value_num IS NOT NULL OR value_text IS NOT NULL)`;
`CHECK (comparator IS NULL OR value_num IS NOT NULL)`. Index
`(workspace_id, metric_id, measured_on DESC)`.

**Column types.** Free-form fields (`title`, both `note`s, `value_text`, `ref_text`) are `TEXT`
and their length limits live **only in the Pydantic schemas**, so a limit changes with a code
deploy and no migration (Postgres stores `varchar(n)` and `text` identically; the `(n)` is only a
check). Short identifier-like fields stay `VARCHAR(n)`: `code`, `unit`, `kind`, `category`,
`source_type`, `source_ref`, and the `name`/`provider` labels. The limits quoted in the tables
above for `TEXT` columns are therefore API validation limits, not column definitions.

**Ordering key everywhere:** `(measured_on, measured_at NULLS FIRST, id)`.

**No stored canonical value.** Conversion to `canonical_unit` happens on read from `value_num`,
`unit` and the metric's `unit_conversions`. There is one source of truth, and fixing a wrong
conversion factor fixes history with no backfill. (Volumes are tens of rows per metric.)

### B. Value, unit and date rules (write path)

- Items take one string field `value`. For a `numeric` metric it is parsed with
  `^\s*(<=|>=|<|>)?\s*(-?\d+(\.\d+)?)\s*$` into `comparator` + `value_num`; anything else is an
  item error (`"Negative"` on a numeric metric tells the caller to use a `text` metric). For a
  `text` metric it is stored in `value_text`. All numbers are strings, per the MCP convention.
- `unit` must equal `canonical_unit`, or be a key of `unit_conversions`, or the item is
  rejected with the registered units listed. **Never guessed, never stored unconverted.** A
  unitless metric accepts only an empty unit.
- `min_value`/`max_value`, when set, reject out-of-bounds numbers (weight keeps its `> 0`
  guarantee through `min_value`).
- `measured_on` defaults to the record's `recorded_on`; for a single log with neither, to today in
  the workspace timezone (resolved the way `app/capture/spending_batch.py` resolves it).
  Future dates beyond tomorrow are rejected. `measured_at`, when given, must fall on
  `measured_on` in the workspace timezone.
- `in_range` is derived on read, never stored: `low` / `normal` / `high` when `ref_low`/`ref_high`
  exist and `comparator` is null; otherwise `null`. It is labelled "per the report's range".

### C. MCP tools (the only generic write path)

Seven new tools. Existing `log_weight` and `get_weight_history` keep their names, schemas and
descriptions (golden catalog stays byte-identical for them) and are re-implemented on the
metric store (§D). Every write commits before returning and has a read-back test.

| tool | scope | purpose |
|---|---|---|
| `record_health_results` | write | **Batch write.** `record?` header `{kind, title, recorded_on, provider?, note?, source_ref?}`, `items[]` (≤100) each `{metric, value, unit?, measured_on?, measured_at?, ref_low?, ref_high?, ref_text?, note?, source_ref?, category?}`, plus `atomic`, `dry_run`, `skip_duplicates`, `create_missing_metrics`, `confirmed`. Always creates or reuses exactly one record, so the free provenance `note` exists for every bulk write |
| `define_health_metric` | write | Create or update a metric: `code`, `name`, `category`, `value_type`, `canonical_unit`, `unit_conversions`, `aliases`, `min_value`, `max_value`. Alias collisions and unit changes on a metric that has data are rejected |
| `list_health_metrics` | read | Catalog with per-metric `count`, `latest` value and date |
| `get_health_series` | read | One metric over `from_date`/`to_date`: points (value, unit, canonical value, comparator, range, `in_range`, record id, note, `source_type`) plus `stats` (latest, min, max, Δ7d, Δ30d, count). Paginated with `total_count`/`next_cursor` |
| `list_health_records` | read | Records by date/kind; `record_id` returns one record with all its measurements |
| `delete_health_measurement` | write | `confirmed: true` required |
| `delete_health_record` | write | Cascades its measurements. `confirmed: true` required; the unconfirmed call returns the measurement count it would remove |

**Batch semantics** (same vocabulary as `log_spending_transactions_batch`):

1. *Validate every item* (metric resolution, value, unit, bounds, dates). *Decide dedupe.*
   *Persist* — unless `dry_run`. An invalid item rejects the whole batch when `atomic`.
2. **Metric resolution** is by `code`, `name` or alias on normalised text. An unknown metric
   gives the item status `unknown_metric` with the nearest catalog names — **not written**. With
   `create_missing_metrics=true` the metric is created from the item (`canonical_unit` = the
   item's unit, `category` = the item's or the record kind); under `dry_run` it reports
   `would_create_metric`, so a duplicated alias (`SGPT` vs `ALT`) is caught before anything exists.
3. **Idempotency:** an existing `record.source_ref` returns that record and matches items against
   *its* measurements, so re-sending a report is a no-op. An exact item `source_ref` match returns
   the existing row. Otherwise the natural key is
   `(metric, measured_on, measured_at, value_num, comparator, value_text, unit)`, counted with
   multiplicity: `duplicate` with `skip_duplicates=true`, else `possible_duplicate` (not
   written). A possible duplicate is never silently skipped.
4. Return `{status, dry_run, record: {public_id, created}, counts: {create, duplicate,
   possible_duplicate, invalid, unknown_metric, would_create_metric}, items: [{index, status,
   match_reason, existing_id, errors, warnings}]}`.
5. `confirmed` follows the repo convention: without it the call previews as `dry_run` would.
6. One audit row per call (`module="health"`, `entity_type="health_record"`, counts and the
   measurement public ids), not one per measurement.

**Prescriptions and other non-numeric results** need no special type: a prescription is a record
`kind="prescription"` with the free note holding the document context and, optionally, one text
measurement per line on a `text` metric (e.g. `prescription_line`: `"Metformin 500 mg, 1-0-1, 30 days"`).
Nothing is seeded for it; the first bulk write defines the metric.

### D. Weight migration

Weight is the metric `body_weight`; the old surface is preserved on top of it.

| existing surface | after |
|---|---|
| `GET/POST /health/weight`, `GET /health/weight/trend`, `DELETE /health/weight/{id}` | Same paths and response shapes (`WeightEntryResponse`, `WeightTrendResponse`), served by the measurement service. `{id}` is the **same public id** (preserved by the copy). The web needs no change |
| MCP `log_weight`, `get_weight_history`; capture/voice `log_weight` | Same names and schemas. Write a `body_weight` measurement with `measured_at = now`, `unit = "kg"`; `source_type` stays `manual` as today |
| Morning-briefing weekly-move line | Reads the `body_weight` series |
| Weekly summary `health_summary.weight_*` | Same fields, from the series |
| Export `health/weight_entries.csv` and `weight_entries` JSON key | **Replaced** by `health/health_metrics.csv`, `health_records.csv`, `health_measurements.csv` (and JSON keys). Breaking for anyone parsing the old file; `SUPPORTED_MODULES["health"]` updated |
| `WeightEntry` model, repository, `weight_repo` plumbing in `jobs.py`, `dependencies.py`, `mcp/support/services.py`, `capture/tools.py`, `summaries/service.py`, `exports/service.py` | Removed in the flip (PR-2); the table is dropped in PR-3 |

**Day-based trend semantics.** Weight deltas move from "timestamp ≤ now − 7 d" to "`measured_on` ≤
latest `measured_on` − 7 days". Results keep their meaning; the boundary shifts by under a day and
the existing trend tests are updated and re-justified in PR-2, not loosened silently.

**Data copy is a script, not a migration** (repo rule: alembic is schema only).
`scripts/migrate_weight_to_health_measurements.py` follows `scripts/audit_brokerage_cash.py`:
dry-run by default, `--apply` in one transaction, idempotent, `import app.core.database.models`.
It ensures `body_weight` exists for each workspace and copies every `weight_entries` row
preserving `public_id`, `created_at`, `updated_at`, `note`, source triplet and `measured_at`,
with `unit="kg"`, `measured_on` = the workspace-local date, `record_id` null. It **refuses** (writes
nothing) when a copied row would disagree with its source, and prints source count, target count
and latest value per workspace so equality can be checked by a read, not by trusting the script's
own output.

### E. REST and web (read-only for the generic store)

REST additions, workspace-scoped, `require_min_role("member")`, paginated:
`GET /health/metrics`, `GET /health/metrics/{code}/series`, `GET /health/records`,
`GET /health/records/{public_id}`. There are no generic write routes, deliberately.

Web (`lifestack-web`, after the API is deployed): a **Records** section on `/health`, below
Weight. A metric list (name, latest value + unit, date, `in_range` chip with the tooltip "per the
report's range"); a metric page with a line chart reusing `WeightTrendChart` patterns, a shaded
reference band where `ref_low`/`ref_high` exist, a points table, and a unit note when points were
converted. A record page lists its measurements, provider, note and `source_ref`. Every row
carries the existing source-type label, so MCP-written data reads as "added via MCP", not "entered
manually". Zod schemas have no `.default()` on required fields; MSW mocks use the real API shape.

## Risks and edge cases

| Risk | Handling |
|---|---|
| Metric sprawl from model-extracted names (`ALT`, `SGPT`, `ALT (SGPT)`) | Aliases resolve before anything is created; unknown metrics are rejected by default; `dry_run` shows `would_create_metric`; `define_health_metric` merges via aliases |
| Wrong conversion factor | Conversion is read-time, so it is fixed once with `define_health_metric`. Changing `canonical_unit` or removing a used unit on a metric with data is rejected |
| Same analyte, different units across labs | Stored as reported, charted in canonical, with a visible "converted from" marker. No registered conversion means no write |
| Threshold values (`<0.5`) in a numeric series | `comparator` kept; excluded from `in_range`, min/max and deltas, but plotted and listed |
| Two legitimate readings the same day | Natural key includes `measured_at`; weight naturally has times, labs naturally dedupe |
| Weight dual-write window at deploy | Script is idempotent; run before and after the flip deploy; old table frozen until PR-3 |
| Sensitive data exposure | Health data is more sensitive than weight. MCP read scope exposes it to any agent holding `mcp:read`; bulk payloads and note text must not appear in application logs or audit `details` beyond ids and counts. Verified in tests, and noted in `docs/SECURITY_CHECKLIST.md` |
| Trust boundary drift | No flags other than the range printed on the report; no wording that implies a diagnosis. Briefing/summary lines state facts only |
| MCP golden catalog | Seven additions land in a deliberate `UPDATE_GOLDEN=1` commit; `log_weight`/`get_weight_history` entries must be byte-identical (tested) |

## Test plan

- **Unit (Red first):** value parser (`<0.5`, `>=100`, junk, text metric); unit validation matrix
  incl. affine conversion (HbA1c % ↔ mmol/mol) and unitless metrics; alias normalisation and
  collision; bounds; `measured_at`/`measured_on` consistency; ordering key; `in_range` with and
  without comparator; series stats excluding comparator rows; day-based Δ7d/Δ30d.
- **Batch tool:** dry-run writes nothing; atomic rejects whole batch; partial success; metric
  resolution by code/name/alias; `unknown_metric` vs `create_missing_metrics` vs
  `would_create_metric`; record `source_ref` re-send is a no-op; multiplicity-aware duplicates;
  100-item cap; concurrent same-`source_ref` sends leave one record (use `race_engine`, remove the
  guard and see it fail). Every write tool is read back through a fresh session.
- **Weight parity:** run the existing `test_health_router` weight tests, the MCP weight tests, the
  briefing and weekly-summary weight tests against the new store. Expected changes are listed in
  the PR, not hidden. Same public id after the copy.
- **Script:** dry run writes nothing; apply copies; second run is a no-op; row drift refuses;
  untouched workspaces stay put.
- **Isolation/RBAC:** workspace isolation and role matrix on every new route and tool.
- **Surface:** golden catalog diff is exactly the seven new tools; seam lint passes; `import app.main`.
- **Export:** the new CSVs and JSON keys include the source triplet and notes.
- **Web (Red first):** metric list, series chart with reference band, converted-unit marker,
  record page, empty states, Zod parse failures surface. Gates: vitest ≥ 70, `tsc --noEmit`, `eslint .`, build.
- **E2E (after both merge):** seed a record via the API, open `/health`, assert the Records
  section, a metric page and the weight section still working.
- Coverage thresholds are not lowered.

## Rollout

Backend and frontend as separate PRs (backend first). The weight move is expand → copy → flip →
contract, because the code and the data cannot change in one atomic step.

1. **PR-1 (api, additive):** migration (next free number after `0073_stale_price_days`;
   renumber on collision), models, repositories, services, REST reads, the seven MCP tools,
   copy script, docs (ERD, PATTERNS, `SECURITY_CHECKLIST`). Weight untouched. Deploy.
   Then on the VM: script dry-run → `--apply` → compare counts and latest value by a read.
2. **PR-2 (api, flip):** weight REST/MCP/capture/briefing/summary/export onto the metric store;
   remove `WeightEntry` plumbing. Run `--apply` immediately before and again after the deploy,
   then verify counts and the latest value per workspace.
3. **PR-3 (api, contract):** drop `weight_entries`. Only after PR-2 has run in production long
   enough to trust, and on explicit go-ahead. Take a verified backup first and remove it once the
   drop is confirmed.
4. **PR-4 (web):** Records section. Auto-deploys on merge; independent of PR-2/3.
5. **E2E** after PR-1, PR-2 and PR-4 are merged.

Nothing is pushed or deployed from this spec without an explicit go-ahead. Each deploy goes
through `./scripts/deploy-production.sh`.

## Decisions for approval

1. **Expand/contract over a one-shot move** for weight (PR-1/2/3 above). Cost: three backend PRs
   and two script runs. Benefit: no window where weight is empty or lost, and a rollback exists
   until PR-3.
2. **Day-based trend deltas** for weight (§D). Alternative: keep timestamp-based deltas by sorting
   on `coalesce(measured_at, measured_on)`; costs a computed ordering expression in every query.
3. **Export breaks** `weight_entries.csv`. Alternative: emit a derived `weight_entries.csv` for one
   release.
4. **Unknown metrics are rejected by default**; auto-creation is opt-in per call.
5. **No stored canonical value** (read-time conversion).
6. **Weight stays kg-only** (spec-069). The conversion mechanism would allow `lb`; adding it is a
   one-line `define_health_metric` change, not part of this spec.
