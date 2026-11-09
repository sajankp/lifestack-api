# Spec-107: Portfolio Report History (append-only table + MCP tools)

**Created:** 2026-11-09
**Status:** Draft - retroactive, pending Sajan approval
**Scope:** API (`app/investing/models.py`, `app/investing/repository.py`, `app/investing/service.py`, `app/mcp/server.py`, `alembic/versions/0072_portfolio_report_history.py`)
**Depends on:** spec-099 capital gains tax estimate (tax buckets), spec-102 LRS summary (headroom), spec-105 look-through (theme weights)

> **Process note.** The model, migration and repository were first written for a card that
> referred to this work as "spec-102"; that number had since been taken by the LRS spec, and
> the report spec document itself was never committed. This is that spec, renumbered, written
> after the implementation. If the approach is rejected, the commits on
> `feat/mcp-next-batch` are the thing to revise.

---

## 1. Problem

The fortnightly portfolio job produces a foundation JSON, an actions markdown and a few
headline numbers, kept only as files under the `personal` profile. There is no queryable
history: comparing theme weights, LRS headroom or tax buckets between runs means re-reading
files. Reports should be stored once, immutably, and be listable by date range.

## 2. Scope

One new table, one repository and service, and two MCP tools. Out of scope: any REST route
or UI, editing or deleting a report, computing report contents (the job does that), and
changing the existing files-on-disk flow.

## 3. Data model

Table `portfolio_reports` (migration `0072_portfolio_report_history`, down-revision
`0071_holding_theme_tags`; purely additive, no existing table touched):

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | internal, never returned |
| `public_id` | uuid, unique | returned to callers |
| `workspace_id` | int FK `workspaces.id`, indexed | every query is workspace-scoped |
| `as_of` | date, not null | report date; the range-filter key |
| `run_id` | varchar(128), not null | run identifier (manifest `run_id`, else `<as_of>_<sha8>`) |
| `scalars_json`, `foundation_json` | jsonb | full payloads for drill-down |
| `actions_md` | text | the actions document |
| `theme_weights` | jsonb | denormalised `{theme: weight}` |
| `harvest_candidates` | jsonb | denormalised list |
| `lrs_headroom_inr` | numeric(18,2) | denormalised |
| `tax_buckets` | jsonb | denormalised `{lt_gain_inr, lt_tax_inr, st_gain_inr, st_tax_inr, total_tax_inr}` |
| `created_at` | timestamptz, default now() | |

Constraints: unique `(workspace_id, as_of, run_id)`; index `(workspace_id, as_of)`.

### Append-only semantics (decision)

Rows are never updated or deleted. `create` uses `INSERT ... ON CONFLICT DO NOTHING` on
`(workspace_id, as_of, run_id)` and then returns the row that actually landed, so a re-send
of the same run returns the existing `public_id` and **never overwrites** it. A new `run_id`
on the same day adds a new row.

An earlier draft of the service described "same `run_id` overwrites" (`DO UPDATE`). That
contradicted the table's purpose (an immutable history) and the migration, so it was
rejected: a corrected report is a new `run_id`.

## 4. Tools

| Tool | Scope | Args | Returns |
|---|---|---|---|
| `create_portfolio_report` | `mcp:write` | `workspace_id`, `as_of` (ISO date), `run_id` (1-128 chars), optional `scalars_json`, `foundation_json`, `actions_md`, `theme_weights`, `harvest_candidates`, `lrs_headroom_inr` (decimal string), `tax_buckets` | `{status, public_id, as_of, run_id, created_at}` |
| `list_portfolio_reports` | `mcp:read` | `workspace_id`, `from_date?`, `to_date?` (inclusive on `as_of`), `limit=20` (1-100), `cursor?` (offset, >= 0) | `{status, total, next_cursor, items[]}`, newest `as_of` first; items carry the denormalised columns plus `foundation_json` and `actions_md` |

Validation failures return `{"status": "error", "message": ...}` and write nothing. The
create payload is capped at 2,000,000 characters of JSON. `total` is returned so a caller
can verify no page was cut off. Authorization failures raise, as for every other MCP tool.

## 5. Known gap: the indexer script

`~/.hermes/profiles/personal/scripts/index_portfolio_report.py` imports `app.*` and writes
through `PortfolioReportService` with a direct database session. It therefore only works
where the Lifestack database is reachable. The production database sits behind the Oracle VM
and the `personal` profile only reaches Lifestack over MCP, so the indexer must be switched
to call `create_portfolio_report` over MCP. That script lives outside this repository and is
tracked on the board, not in this change.

## 6. Tests

- `app/tests/investing/test_portfolio_report_repository.py` - repository against the migrated
  testcontainer database (also proves the migration applies).
- `app/tests/test_mcp_portfolio_reports_db.py` - the two tools end to end: round trip of every
  column, append-only (same `run_id` never overwrites), new `run_id` same day, inclusive range
  filter, newest-first ordering, complete pagination, workspace scoping, scopes requested
  (`mcp:write` / `mcp:read`), input validation, payload cap, decimal precision, and
  authorization failure writing nothing.
- `app/tests/test_mcp_tools_suite.py` - both tools registered.

## 7. Deployment

One additive migration (`0072`); rollback is `alembic downgrade 0071` (drops the new table
only). Take a database dump before deploying, as for any migration.
