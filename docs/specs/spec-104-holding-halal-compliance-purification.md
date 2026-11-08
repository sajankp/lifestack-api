# Spec-104: Per-Holding Halal Compliance Verdicts and Purification Estimates

**Created:** 2026-11-09
**Status:** Draft - retroactive, pending Sajan approval
**Scope:** API (`app/investing`, `app/mcp/server.py`, `alembic/versions/0070_holding_compliance.py`)
**Depends on:** spec-073 dividends, spec-041 orders/holdings, spec-083 security identity
**Kanban:** t_84367d75

> Retroactive: the implementation was written before this spec existed. Every decision below is the implementer's proposal and is **not approved**. Nothing here is a religious ruling.

---

## 1. Problem

Halal screeners (Musaffa, Zoya, AAOIFI-based tools, S&P Shariah, ...) disagree on the same stock, and their numbers change quarterly. Lifestack holds the user's positions and dividends but has no place to record "is this holding acceptable, according to whom, as of when", nor to answer "how much of my dividends do I give to charity?".

## 2. Goals / non-goals

Goals
1. Store one compliance verdict per holding with its **sources** (screener name, URL, rule reference) and **date**.
2. Keep disagreeing screeners **visible**, never merged silently.
3. Return a purification estimate for a single dividend and for a holding's income, with a clear basis.
4. Expose via REST and MCP.

Non-goals: fetching screener data automatically, scraping, a fatwa engine, FX conversion, tracking whether the purification was actually paid, UI.

## 3. Decisions

| Topic | Decision | Why |
|---|---|---|
| Verdict set | `halal`, `non_halal`, `permissible`, `unknown` | `permissible` = not fully halal but acceptable after purification (the case that needs an estimate); `unknown` = not yet reviewed. Stored as `varchar` + `CHECK`, not a PG enum, so a value can be added without enum DDL. |
| History | **Single current row per holding, latest write wins**; history is the audit log (`entity_type=holding_compliance`, `before`/`after` on every change) | Avoids a second table and "which row is current" logic. Trade-off: no queryable history endpoint. |
| Screeners disagree | Each source may carry its own `verdict` and `as_of_date`; response has `sources_disagree` (true when sources carry >1 distinct verdict). The top-level verdict is the user's own call. | Disagreement stays inspectable; the app does not pick a winner. |
| Percentage | `impure_income_pct`, Decimal 0-100, 4 dp (`numeric(7,4)` + `CHECK`). | Screeners publish percentages. Optional. |
| Purification base | **Gross** income received (before withholding tax), summed per holding in SQL | Purification is owed on income earned. Conservative; open question 1. |
| Rounding | Half-up to the cent | Money is `Decimal`, serialised as strings. |
| No percentage / no record / dividend not linked to a holding | amount `0.00` with the reason in `basis`, `compliance_on_file` flag | Never invent a number. |
| Other-currency income | Excluded from a holding total and listed in `assumptions` (no FX) | Avoids unsourced FX. |
| Disclaimer | `disclaimer` field on every estimate: "Estimate only, not a fatwa ..." | |
| Future dates | `as_of_date` (and per-source dates) rejected when later than tomorrow (UTC) | One day of slack for users ahead of UTC. |
| Source validation | 1-10 sources; `screener_name` <=100, `rule_reference` <=500, `url` absolute `http`/`https`, no whitespace, <=2048 chars | Verifiable citations; blocks `javascript:`/`ftp:` etc. |
| Tenancy | Every query filters by `workspace_id`; unique `(workspace_id, holding_id)`; foreign holding or dividend -> 404 (not 403) | `investing_holdings` has no `(id, workspace_id)` unique key, so a composite FK is not possible without altering that table; the service resolves the holding workspace-scoped before every write. |
| Concurrency | `INSERT .. ON CONFLICT (workspace_id, holding_id) DO UPDATE` | Idempotent and race-free. |

## 4. Data model

`investing_holding_compliance` (migration `0070_holding_compliance`, down_revision `0068_capital_gains_tax` on this branch; the integrator re-points it):

| column | type | notes |
|---|---|---|
| id, public_id | int / uuid | `public_id` unique |
| workspace_id | int FK workspaces | |
| holding_id | int FK investing_holdings, `ON DELETE CASCADE` | indexed |
| verdict | varchar(20) | CHECK in the four values |
| sources | JSON (list of objects) | `screener_name, url, rule_reference, verdict?, as_of_date?` |
| impure_income_pct | numeric(7,4) null | CHECK 0-100 |
| as_of_date | date | verdict date |
| created_at, updated_at | timestamptz | |

Unique `(workspace_id, holding_id)`. Compliance disappears with its holding.

## 5. API

- `PUT /v1/investing/holdings/{holding_id}/compliance` (role >= member) - body `{verdict, sources[], as_of_date, impure_income_pct?}`; upsert; 200 with record. 404 foreign/unknown holding; 422 validation.
- `GET /v1/investing/holdings/{holding_id}/compliance` - 404 when no record.
- `GET /v1/investing/purification/estimate?holding_id=|dividend_id=[&from_date=&to_date=]` - exactly one id (422 otherwise); dates only for holding scope.

Estimate response: `scope, target_public_id, holding_public_id?, verdict, compliance_on_file, verdict_as_of_date?, impure_income_pct?, base_amount, purification_amount, currency, dividend_count, basis, assumptions[], disclaimer`.

## 6. MCP tools

| Tool | Scope | Args |
|---|---|---|
| `set_holding_compliance` | `mcp:research` + `confirmed` flag | `workspace_id, holding_id, verdict, sources[], as_of_date, impure_income_pct?, confirmed=false` |
| `get_holding_compliance` | `mcp:read` | `workspace_id, holding_id` (`compliance` is null when none) |
| `get_purification_estimate` | `mcp:read` | `workspace_id, holding_id? , dividend_id?, from_date?, to_date?` |

## 7. Open questions for approval

1. Gross vs net dividend as the purification base (some scholars use net); gross chosen as conservative.
2. Should a `questionable` verdict exist separately from `unknown`/`permissible`?
3. Is a queryable history table wanted instead of the audit log?
4. Should dividends on exited positions (no holding row) be attributable via a symbol-keyed compliance record?
5. Should `mcp:research` be the write scope (as requested on the card) or the standard `mcp:write`?

## 8. Test plan

DB-backed tests in `app/tests/investing/test_holding_compliance.py`: round trip with sources, disagreement flag, idempotent upsert + audit, 404s, cascade, cross-workspace 404 on every endpoint, parametrised 422 validation, DB constraints, estimate for dividend / holding / date window / no ratio / no record / unlinked dividend / non_halal, and the three MCP tools (confirmation gate, scopes, errors, docstrings).
