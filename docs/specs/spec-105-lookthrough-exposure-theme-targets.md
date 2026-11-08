# Spec-105: Look-through Exposure and Theme Targets

**Created:** 2026-11-09
**Status:** Draft - retroactive, pending Sajan approval
**Scope:** API (`app/investing/theme_*.py`, `app/investing/models.py`, `app/mcp/server.py`, Alembic `0071_holding_theme_tags`)
**Depends on:** spec-012 look-through and overlap analytics, spec-075 display FX rule, spec-085 value-weighted coverage
**Branch:** `feat/lookthrough-exposure-theme-targets`

This spec was written after the implementation (kanban `t_ab6a79f2`). It records the decisions
made while building it and has not been approved.

---

## 1. Problem

Funds (ETFs, mutual funds) hide what the portfolio really owns, and themes the user cares about
(AI power/grid, battery, robotics) cut across sectors and funds. Today the user can see
company-level look-through (`/analytics/exposure`) but not a roll-up by sector, country or theme,
and cannot ask "how far is each theme from its target, and where should this month's new cash go
without selling anything?".

## 2. Scope

1. `get_lookthrough_exposure` - exposure aggregated by **company / sector / country / theme**.
2. **Theme tags** on holdings, each with an optional target weight.
3. `get_allocation_vs_target` - theme **drift %** and **suggested new-cash deployment per theme**
   (the "no sell to rebalance, use new cash" rule).

Out of scope: setting a company's sector (spec-100, a separate card), automatic theme
classification, sell/trim suggestions, per-holding purchase suggestions.

## 3. Data model (migration `0071_holding_theme_tags`)

Table `investing_holding_theme_tags`:

| Column | Notes |
|---|---|
| `id`, `public_id` | standard; `public_id` unique |
| `workspace_id` | FK `workspaces.id`; every query filters by it |
| `holding_id` | FK `investing_holdings.id`, `ON DELETE CASCADE` |
| `theme` | `varchar(64)`, whitespace-collapsed |
| `target_weight_pct` | `numeric(5,2)`, **nullable**, check `0..100` |
| `created_at`, `updated_at` | |

Unique `(holding_id, theme)`; index `(workspace_id, theme)`. Downgrade drops the table.
The migration revises `0068_capital_gains_tax` on this branch; the integrator re-points it.

### Decisions

- **Many-to-many.** A holding may carry several themes and a theme may span several holdings.
- **Target semantics.** `target_weight_pct` is the share of the **whole portfolio** that this
  holding is meant to contribute to the theme. A theme's target is the **sum** of its tags'
  targets. A tag without a target only groups exposure (no drift, no cash suggested).
- **Targets are percentages of the portfolio and may total less than 100.** The remainder is
  reported as `unallocated_target_pct`. A write that would make **all** theme targets in the
  workspace total more than 100 is rejected with **422** and nothing is written.
- **Theme spelling.** Themes are trimmed and whitespace-collapsed; a theme already used in the
  workspace keeps its existing spelling (case-insensitive match). Duplicate themes on one holding
  (case-insensitive) are rejected.
- **Replace semantics.** Setting tags replaces the full tag set of each listed holding (empty list
  clears it); unlisted holdings are untouched. Re-sending the same set is idempotent and keeps tag
  identities. A bulk request is validated up front and written atomically.

## 4. Look-through rules

- **One snapshot per fund.** For each ETF/MF the single latest constituent snapshot on or before
  `as_of_date` is used: newest by `(as_of_date, fetched_at)`, ties broken by source name. Rows of
  other sources/dates are ignored, so duplicate snapshots never double count. (The existing
  `get_latest_on_or_before_many` returns every source on the newest date, hence the new
  `LatestConstituentSnapshotRepository`.)
- **Direct stocks count as themselves** (company via the instrument's `company_id`).
- **Not itemised bucket** (key `not_itemised`, shown in every grouping; **never rescaled to 100**):
  - holdings whose symbol starts with `OTHER-` (case-insensitive);
  - constituent rows whose company ticker/name starts with `OTHER-` (how the snapshot writer
    stores "everything else") or whose company cannot be found;
  - the part of a fund the snapshot does not cover (`1 - sum(weights)`);
  - funds with no snapshot, instruments that cannot be resolved, stocks not linked to a company.
- Weights are decimals 0..1 as stored. If a snapshot's weights total more than 1 they are scaled
  down to 1 (warning if the excess exceeds 0.0001). Weights are never scaled up.
- A stale snapshot (older than 90 days) is still used, with a warning.
- Sector and country come from the company (`sector`, `country_code`); missing values group under
  `Unclassified` / `Unknown`, distinct from `not_itemised`.
- **Themes are holding-level**: a holding counts fully toward each of its tags, so theme rows
  overlap and may total more than 100%. `unthemed_value` is the value of holdings with no tag.

## 5. Valuation and money

- Value = quantity x latest price on or before `as_of_date`, converted to the workspace reporting
  currency with the existing FX helpers (previous-day close rule, spec-075). With no configured
  reporting currency: the sole holding currency, else **INR** with a warning.
- A holding with no price, or with no FX path to the reporting currency, is **excluded with an
  explicit warning** and `analysis_status` becomes `partial`; it is never valued at zero silently.
- All arithmetic is `Decimal`. Amounts are rounded to cents and percentages to 2 dp. Partitions
  (company atoms, sector, country) are rounded with the **largest-remainder method** so they sum
  exactly to `total_value`. Theme rows overlap and are rounded independently.
- Empty portfolio and zero total value are supported: every percentage is 0, nothing divides by
  zero.

## 6. Allocation vs target

Inputs: portfolio value `V` (reporting currency), `new_cash` `C` (>= 0, at most 2 dp, finite;
otherwise **422**), `T = V + C`.

- `current_pct = theme value / V x 100` (0 when `V = 0`).
- `drift_pct = current_pct - target_pct` (negative = under-weight). Null for themes without a target.
- `shortfall = max(0, target% x T - theme value)` - measured against the portfolio **including**
  the new cash.
- **Deployment:** if `C` covers the total shortfall each theme receives its shortfall; otherwise `C`
  is split in proportion to shortfall. Each suggestion is **capped at its shortfall**, **never
  negative** (nothing is ever sold), rounded down to cents with remaining cents handed out by
  largest remainder, and `total_suggested + unallocated_cash = new_cash` exactly.
- Themes are listed most under-weight first; untargeted themes last.
- Limitation: because a holding may be in several themes, one purchase can reduce several
  shortfalls; suggestions are per theme and a warning is emitted when targeted themes overlap.

## 7. Interfaces

REST (under `/v1/investing`):

| Route | Notes |
|---|---|
| `GET /analytics/lookthrough-exposure?as_of=&group_by=` | `group_by` in `company, sector, country, theme`; omitted = all |
| `GET /analytics/allocation-vs-target?as_of=&new_cash=` | `new_cash` default 0 |
| `PUT /holdings/theme-tags` | bulk replace, role `member` |
| `PUT /holdings/{holding_id}/theme-tags` | one holding, role `member` |
| `GET /holdings/{holding_id}/theme-tags` | tags plus workspace theme targets |

MCP tools:

| Tool | Scope | Args |
|---|---|---|
| `set_holding_theme_tags` | `mcp:research`, `confirmed` gate | `workspace_id`, `holdings: [{holding_id, tags: [{theme, target_weight_pct?}]}]`, `confirmed` |
| `get_lookthrough_exposure` | `mcp:read` | `workspace_id`, `as_of_date?`, `group_by?` |
| `get_allocation_vs_target` | `mcp:read` | `workspace_id`, `new_cash="0"`, `as_of_date?` |

## 8. Verification

`app/tests/investing/test_theme_exposure.py` covers: single-snapshot selection with duplicate and
stale snapshots; exact sums; placeholders and uncovered remainder; group_by; empty portfolio;
missing FX and FX conversion; tag CRUD, idempotency and spelling canonicalisation; >100% rejection
with atomic rollback; validation errors; cross-workspace isolation; drift, proportional deployment,
capping and leftover; untargeted themes; zero-value portfolio; invalid `new_cash`; MCP tools
including scope and confirmation gating.

## 9. Open questions

- Should a theme target be a single per-theme number rather than the sum of per-holding targets?
- Should purchases be suggested per holding, not just per theme?
- Should themes be attachable to companies (for look-through into funds) rather than holdings only?
