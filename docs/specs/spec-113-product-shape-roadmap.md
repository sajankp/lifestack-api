# Spec-113: LifeStack product shape — Today, Money, Investments (roadmap + phases 2–5)

**Status:** Approved by the owner (2026-11-28); not yet implemented.
**Scope:** `lifestack-api`, `lifestack-web`, `lifestack-e2e`, plus one hand-off card to the Hermes
`personal` profile.
**Source:** the product review `/home/sajan/lifestack/.hermes/plans/2026-11-28_161148-lifestack-product-shape.md`,
which has the evidence, screenshots and reasoning. This spec is the buildable part.
**Sub-specs:**
- **spec-114**: tax set-off and carry-forward (track T, can run alongside phase 1)
- **spec-115**: phase 1 (budget rule, actionable push, Saturday push, period change)

---

## 0. Owner decisions (2026-11-28)

| Topic | Decision |
|---|---|
| Navigation | 5 items: **Today · Money · Investments · Tasks · Health** (+ Settings). No "Review" page. |
| Weekly summaries | No page. A Day/Week/Month/Year switch on net worth and investments, plus a **Saturday-morning** push. Summaries keep generating in the background for voice/MCP. |
| Budget rule | Spending in categories without a budget is its own line, never inflating "used %". |
| Custom KPIs | Folded into Budget (the owner never set one). |
| Money default | Opens on **Activity**. |
| Spending charts | Kept as a Money tab (used occasionally). |
| Devices | Phone and iPad mostly: bottom bar on phone, 5-icon rail on iPad. |
| Morning push | Only actionable items; category-spike alerts off. |
| Transfers | One row per transfer in "All accounts". |
| Hermes report | LifeStack computes ledger facts live. `personal` pushes judgement and outside data only (themes, holding roles, planned exits as tasks, the actions note). Themes stay in personal. |
| ELSS | Per-instrument lock-in setting (no ELSS category exists today). |
| Tax losses | Set off against the **highest-taxed gain first** (spec-114). |

## 1. Phases and order

Dependencies (the delivery is sequential anyway; see §7):

```
P1 (spec-115) → T (spec-114) → P2 → P3 Today → P4 Money + nav → P5 Investments → personal's pending items
```

| Phase | Spec | Repos | Needs done first |
|---|---|---|---|
| **P1** | spec-115 | api → web → e2e | — |
| **T** | spec-114 | api | — |
| **P2** | §2 below | api → web → e2e | — |
| **P3** | §3 | web (api from P1) → e2e | P1 |
| **P4** | §4 | api (small) → web → e2e | P2, P3 |
| **P5** | §5 | api → web → e2e | T, P4 |
| personal | §6 | card `t_7ae94ff5` (now) + pending README items | P5 live (pending items) |

**Rules for every phase** (from the root `CLAUDE.md` and the `lifestack` skill):
- **One branch and one PR per repo for the whole programme** (§7). E2E is written at the very end, after the
  API and web PRs merge, because E2E CI checks out the
  sibling repos' `main`.
- Branch `feat/lifestack-reshape` in each repo, in worktrees under `/home/sajan/lifestack-wt/reshape/`
  (§7). Never switch the shared checkout's branch.
- TDD, coverage thresholds never lowered, and `pre-commit run --all-files` green before any commit is
  called done.
- Commit freely per task (TDD). Push, PRs, merge and deploy happen once, at the end (§7), with the owner's
  go-ahead. Merge with `--merge`; squash is disabled.
- No secrets in output. Prod (Oracle VM, 1 GB) is memory-bound: no new page may call investing analytics
  endpoints on load unless that page is Investments.
- Don't paste pages together. Extract sections into their own files.

## 2. Phase 2: one-row transfers, plain words, no duplicate buttons

### 2.1 API: one row per transfer in "All accounts"
- `app/finance/activity_service.py` ~L176-222: when **no account filter** is set, emit **one** item per
  transfer instead of an out-row and an in-row:
  - `id = uuid5(NAMESPACE_OID, f"transfer:{public_id}")`
  - `event_type="transfer"`, `description=f"{from} → {to}"`
  - `amount` = the outgoing amount, negative, in the from-currency
  - `account_*` = from-account, `counterpart_*` = to-account
  - new optional fields on `ActivityFeedItem` (`app/finance/activity_schemas.py`): `amount_in` (str) and
    `currency_in`, the received side, which differs from `amount` when FX applies
- With an account filter, behaviour is unchanged: that account's side only, same ids as today.
- Check every consumer of the old `:outflow`/`:inflow` ids. `MoneyFlowPage.tsx` delete/edit uses
  `event_type === 'transfer'` and `source_ref`, so it should be unaffected; verify. Also check any
  running-balance code before changing anything.
- Tests:
  - All-accounts feed → 1 item per transfer with both amounts
  - single-account feed → unchanged
  - FX transfer → `amount_in` / `currency_in` set

### 2.2 Web: wording, formatting, duplicates
- **Copy** (exact replacements):

  | Now | New |
  |---|---|
  | "Money Flow & Architecture" | "Money" |
  | "Unified financial ecosystem…" | "Your accounts, balances and activity." |
  | "Account Architecture" | "Accounts" |
  | "Unified Activity Stream" | "Activity" |
  | "Runaway burn rate" / "Burn velocity split" | "On pace to spend ₹X" / "Fixed vs. flexible spending" |
  | "Bulk Imports" / "Data Exports" | "Import" / "Export" |
  | "Portfolio valuation status is 'estimated'" | "Some prices are estimates (updated <date>)" |

- **Transfer row** in `src/components/finance/ActivityFeedTimeline.tsx` (~L108): when `amount_in` is
  present, show "Wallet → Brokerage" with one amount (both amounts if the currencies differ). Badge
  "Transfer".
- **Trade rows**: "Bought 6 VTI @ $265.00" instead of `BUY VTI (6.00000000 @ 265.000000)`. Trim trailing
  zeros from the quantity and format the price with the currency formatter.
- **One Transfer button** on the Money page: keep the header one, remove "Transfer Funds" in the account
  map and the "Transfer between accounts →" link.
- **Capture**: remove the sidebar item (`components/layout/constants.ts`, `to: '/capture'`). The header mic
  and the floating button stay. Remove `nav-capture` usage in the drawer.
- **One valuation line** on Investments: derive "Valuation status" and "Valuation as of … · estimated" from
  the same status, so they can't contradict each other.
- Vitest per component.
- E2E: update assertions on changed copy and the transfer row. Record each changed selector in the step report; the E2E work happens at the end (§7.3).

## 3. Phase 3: Today (rebuilt Dashboard)

Uses `/finance/period-change` and the actionable briefing from spec-115. No new heavy API calls.

**Layout** (`src/pages/DashboardPage.tsx`, sections extracted to `src/pages/today/*.tsx`):
1. **Net worth**: value, the `[Day · Week · Month]` switch (default Week), the change, and on Week/Month
   the split line "₹A saved · ₹B markets & other". Under it, an Investments line: change plus "₹X new
   money · ₹Y growth". Partial/unavailable → totals only, with a small note.
2. **Needs you**: the briefing's actionable lines (deep links), or a single "All clear ✓". Never both.
3. **This month**: spent · left in budget · not covered by a budget (from `MonthBudgetStatus` via the
   dashboard summary).
4. **Next up**: up to 3 tasks/doses with inline tick-off (existing todo/health mutations).
5. **Good to know** (collapsed by default): the briefing's non-actionable lines.

**Remove:**
- the KPI grid
- the standalone cue cards (`dashboard-cue-*`)
- "Workspace snapshot" and "Live totals…"
- the duplicate monthly-spend figures

Keep `dashboard-portfolio-value` as the test id on the Investments line.

Rename the nav label to **Today**; the route stays `/`.

**API** (small, in this step's API commits if needed): add `month_budget` (from `MonthBudgetStatus`) to
`DashboardSummary` so Today needs no extra call. It goes through the same Redis cache.

**Tests:**
- Vitest for each section, including the all-clear vs. needs-you exclusivity, the partial period change
  and the empty states.
- E2E after merge: Today shows net worth and the Week switch; "All clear" and "Needs you" are never both
  visible.

## 4. Phase 4: Money, Budget, navigation

### 4.1 Money page (`/money`, opens on Activity)
- **Tabs** (routes): `/money` (Activity) · `/money/accounts` · `/money/budget` · `/money/spending`.
- **Shell:** `src/pages/money/MoneyPage.tsx`, header with net worth · cash · invested and the **Add ▾**
  menu.
- **Sections:** `ActivitySection.tsx`, `AccountsSection.tsx`, `BudgetSection.tsx`, `SpendingSection.tsx`.
- **Extract, don't rewrite:**
  - `MoneyFlowPage.tsx` (~800 lines) → Activity + Accounts
  - `SpendingPage.tsx` (~1.6k lines) and `pages/spending/*Tab.tsx` → Budget (budgets + recurring) and
    Spending (analytics)
- **Add ▾** menu: Expense · Income · Transfer · Buy/Sell (opens the order form directly) · Dividend ·
  Bill (recurring) · Import · Past data.
- **Budget tab:**
  - category and group lines with pace bars
  - a "Not covered by a budget" line
  - an optional **overall monthly limit** (4.2)
  - recurring bills with "due in N days"
  - clicking a budget line opens Activity filtered to that category (query string)
- **Accounts tab:** grouped everyday / cards / brokerage, the net-worth chart with Week/Month/Year/All,
  "Manage accounts" (deep link to Settings ▸ Accounts), and the **default spending account** setting (moved
  from Settings ▸ Currency & Display).

### 4.2 Overall monthly limit + KPIs folded into Budget (API)
- Alembic migration:
  - relax `ck_budget_scope` (`app/spending/models.py` ~L255) to allow *both* `category_id` and
    `category_group_id` NULL = **overall**
  - partial unique index: one overall row per `(workspace_id, start_month)` range (validate overlaps in
    the service)
- `MonthBudgetStatus` (spec-115) gains an `overall` line covering **all** spend.
- **KPIs:**
  - prod has no custom KPIs. Verify with a count query on `financial_kpis` before the migration and record
    the result in the step report (and later the API PR description).
  - remove the KPI UI
  - stop scheduling `kpi_guardrails_job` (`app/main.py` ~L163) behind a setting
    `KPI_GUARDRAILS_ENABLED=False`
  - keep the table and the API for one release, then drop them in a later chore
- Tests:
  - overall line covers unbudgeted spend
  - migration up/down
  - the KPI job isn't registered when disabled

### 4.3 Navigation
- `components/layout/constants.ts` → five items: Today (`/`, `nav-dashboard`), Money (`/money`,
  `nav-money`), Investments (`/portfolio`, `nav-portfolio`, label "Investments"), Tasks (`/todo`,
  `nav-todo`), Health (`/health`, `nav-health`). Settings stays pinned.
- **Removed:** `nav-spending`, `nav-summaries`, the Import/Export items, `nav-capture` (if not already gone
  in P2).
- **Phone (< 768 px):** a bottom bar with Today · Money · Investments · Tasks · More (Health, Settings).
  Replaces `MobileNavDrawer.tsx` as primary navigation.
- **iPad (768–1023 px):** the existing icon rail (`sidebarDefault.ts`), now with 5 icons. Follow
  `lifestack-web/docs/responsive-layout-decision.md` and update it.
- **Settings ▸ Data:** Import and Export pages move under `/settings/data` (two cards).

### 4.4 Redirects (permanent; old links live in stored notifications and API text)
`src/App.tsx`:

| From | To |
|---|---|
| `/spending` | `/money/budget` |
| `/spending?tab=budgets` | `/money/budget` |
| `/spending?tab=recurring` | `/money/budget` |
| `/spending/budgets`, `/spending/recurring`, `/spending/kpis` | `/money/budget` |
| `/spending/analytics` | `/money/spending` |
| `/summaries` | `/` (Today, Week selected) |
| `/imports/*` | `/settings/data` |
| `/exports` | `/settings/data` |
| `/investing/*` | `/portfolio/*` (already exists; keep) |
| `/net-worth` | `/money/accounts` |

Add a Vitest test that walks every route the API emits and asserts it resolves to a real page, never the
`*` → `/` fallback:
- `app/application/workflows.py` `route=` values
- `_INSIGHT_ENTITY_ROUTES`

Then, in this step's API commits, update those emitters to the new routes. That's a tidy-up; the redirects
stay.

### 4.5 E2E selectors removed or changed (record them in the step report and the final web PR)

The counts are usages in `lifestack-e2e` today:
- `nav-spending` ×13
- `nav-summaries` ×5
- `spending-tab-recurring` ×4, `spending-tab-budgets` ×3, `spending-tab-kpis` ×2,
  `spending-tab-analytics` ×1
- `header-quick-spending` ×7 (keep the id on the Add ▾ → Expense item)
- `summary-stale-indicator` ×1
- `nav-mobile-open` ×2 (replaced by the bottom bar)

Keep these ids where the element survives:
- `spending-recurring-*`, `spending-budget-save` (move with the component)
- `nav-money`, `nav-portfolio`, `activity-feed-*`

## 5. Phase 5: Investments (incl. the Hermes report and the tax UI)

### 5.1 Structure
- **Header strip** (replacing ~900 px of cards and blocks): Value · Change [Day · Week · Month · Year]
  (from `/finance/period-change` investments) · Gain since bought · Cash to invest. Plus one valuation badge
  "Prices from <date>" (amber when estimated).
- **Tabs:** Holdings · Orders · **Performance** · **Exposure** · **Tax & dates**. The current Analytics tab
  (`src/pages/investing/AnalyticsTab.tsx`, 3,284 px tall) is split into Performance and Exposure.

### 5.2 Performance
- "Hermes note · <as_of>" card at the top. Collapsible; shows the latest stored report's `actions_md`
  rendered as markdown (REST `GET /investing/portfolio-reports` exists, `app/investing/router.py` ~L1072; check
  whether the list includes `actions_md` or whether the detail call `/investing/portfolio-reports/{id}` is
  needed).
- Then returns, with **every figure labelled** ("Gain on what you hold now", "Annualised return (XIRR)").
  The nested duplicate "0.4% · held 20d" is removed.
- Performance chart and dividends.

### 5.3 Exposure
- Look-through with plain labels: "Whole portfolio, seen through funds ₹27.5L · of which stocks you hold
  directly ₹13.1L". A zero direct value shows "—".
- Overlap.
- **Themes** from the pushed `theme_weights`, with readable names (`semis_ai` → "Semis & AI"; map plus
  title-case fallback) and the gap explained ("84% grouped, 16% not itemised").
- Data warnings collapse into one line ("3 funds have stale data — view").
- The instrument admin list moves to Settings ▸ Instruments, together with the constituent threshold
  setting.
- **Remove** the report cards that copy LifeStack numbers (`PortfolioReportCards.tsx`: `TaxBucketsCard`,
  `LrsHeadroomCard`, `HarvestCandidatesCard`) and the empty `CountrySplitCard`. `ThemeSplitCard` moves here.
- **iPad width (768–1180 px):** at most two columns; tables scroll horizontally inside their card and are
  never clipped; legends wrap.

### 5.4 Tax & dates (API from spec-114; new API here)
- **This year so far:** the four buckets with one line each: gains → after losses set off → after the
  ₹1.25L exemption → tax. Then the set-off steps as sentences (spec-114 `set_off_steps`), unabsorbed losses,
  and brought-forward losses used.
- **Ways to pay less** (new API `GET /investing/tax/opportunities`, `TaxWhatIfService` +
  `tax_from_buckets`, ranked by ₹ saved this FY):
  - **Harvest losses:** spec-114 harvest candidates, marginal value.
  - **Wait for long-term:** open lots turning long-term within 60 days, with tax saved by waiting (engine
    diff: sell today vs. on the long-term date).
  - **Use the ₹1.25L exemption:** unused exemption = 125,000 − Indian long-term gains after set-off, with
    the Indian long-term-eligible lots whose gains fit. Only if > 0 and before 31 Mar.
- **Before you sell:** pick a holding and quantity → today vs. the long-term date, lot by lot
  (`POST /investing/tax/simulate-sale`).
- **LRS:** used / headroom live from `/finance/lrs/summary`.
- **Dates** (new API `GET /investing/tax/dates`; these also feed the briefing as actionable lines when due
  within 7 days):
  - advance-tax instalments from the estimate (15 Jun 15%, 15 Sep 45%, 15 Dec 75%, 15 Mar 100%;
    outstanding = cumulative share of `total_tax_inr` − recorded advance tax, shown as an estimate)
  - LRS 180-day marks on foreign sale proceeds that weren't reinvested
  - lots turning long-term
  - ELSS lock-in ends (5.5)
- **Tax settings** (profile, carry-forward rows from spec-114) are edited in Settings ▸ Tax, with a link
  from here.

### 5.5 ELSS lock-in
- Migration: `lock_in_months` (int, nullable) on the workspace instrument/holding record. The agent checks
  which table holds per-instrument workspace settings; Settings ▸ Instruments edits it.
- **Suggest automatically** when the fund name matches `/\b(ELSS|Tax Saver)\b/i`. The owner confirms; it's
  never set silently.
- A lot is locked until `acquired_at + lock_in_months`, **per purchase** (each SIP instalment separately).
- Locked lots are **excluded** from harvest candidates, "wait for long-term" and sell simulation (the
  simulation states "N units locked until <date>").
- Tax treatment is unchanged: equity MF, Indian 111A/112A.
- Tests:
  - per-lot lock dates
  - exclusion from harvest
  - suggestion regex
  - lock-in end appears in `/investing/tax/dates`

## 6. The `personal` profile's part (kanban card `t_7ae94ff5`, created 2026-11-28)

`personal` owns its scripts (`~/.hermes/profiles/personal/scripts/`); the coding profile doesn't edit them.
The single card `t_7ae94ff5` carries everything personal needs:
- **now:** post the tax-bucket evidence for spec-114, write planned exits/folds as LifeStack todos
  (category `portfolio-plan`, idempotent via a `[plan:SYMBOL:date]` key), move the cron to Saturday 04:30 IST
  and rename it
- **deferred:** recorded in personal's own `portfolio/README.md`. After §5 is live, Sajan tells personal to
  carry out the pending items

The deferred items, for reference (originally drafted as a separate card):

> **Goal:** stop pushing numbers LifeStack now computes live; push judgement as tasks.
> **Context:** spec-113 §5/§6 in `lifestack-api/docs/specs/`. LifeStack now shows tax buckets with
> set-off, LRS headroom and harvest candidates live, plus tax dates.
> **Do:**
> 1. In `run_foundation.py` / `index_portfolio_report.py`, stop sending `tax_buckets`, `lrs_headroom_inr`
>    and `harvest_candidates` to `create_portfolio_report`. Keep `theme_weights`, `actions_md` and
>    `foundation_json`.
> 2. For each planned exit/fold in `portfolio/config/holding_roles.json`, create or update a LifeStack todo
>    with the due date and a stable source ref (idempotent; no duplicates on re-runs). Mark it done when the
>    position is gone.
> 3. Move the cron to Friday night or early Saturday IST, so Telegram and the Saturday push arrive
>    together. Rename `fortnightly-portfolio-foundation` (it runs weekly).
> 4. Optionally drop from the Telegram report the sections now live in LifeStack (tax, LRS, cash). Your
>    call.
> 5. If brought-forward losses are known from the last ITR, record them with `set_tax_loss_carry_forward`
>    (spec-114).
>
> **Done when:** one run stores a report without the three fields, the todos exist in LifeStack once each,
> and the cron shows the new schedule and name.

## 7. Delivery: one coding agent, one branch and one PR per repo

The owner runs a **single coding agent** through the six steps below, **in order**.
- All work collects on one branch per repo, `feat/lifestack-reshape`, with as many commits per feature as
  TDD needs. Each commit message carries the step tag, e.g. `fix(tax): … (spec-114 T1)`, so the final PR
  reads feature by feature and any feature can be reverted on its own.
- Nothing is pushed or merged until all six steps are done.
- At the end there is **one API PR and one web PR**, then **one E2E PR**.

### 7.1 Workspace (create once, reuse for every step)

The three worktrees sit side by side, so `lifestack-e2e/docker-compose.e2e.yml` (build contexts
`../lifestack-api`, `../lifestack-web`) builds the feature branches, not `main`:

```bash
mkdir -p /home/sajan/lifestack-wt/reshape
for r in lifestack-api lifestack-web lifestack-e2e; do
  git -C /home/sajan/lifestack/$r fetch -q origin
  git -C /home/sajan/lifestack/$r worktree add /home/sajan/lifestack-wt/reshape/$r -b feat/lifestack-reshape origin/main
done
export PY=/home/sajan/lifestack/lifestack-api/.venv/bin/python
export PRECOMMIT=/home/sajan/lifestack/lifestack-api/.venv/bin/pre-commit
```

- Web needs `node_modules` in its worktree (`npm ci`), and so does E2E.
- Never use the shared checkouts in `/home/sajan/lifestack/`, which may be on other branches.

### 7.2 Steps

| Step | Spec | Repos touched | Local check before moving on |
|---|---|---|---|
| 1 | spec-115 (budget rule, actionable push, period change, Saturday push) | api, web | The seeded budget case gives one answer across the four places (test). A local run of `morning_briefing_job` and `weekly_wealth_push_job` against the e2e stack writes the expected notifications. |
| 2 | spec-114 (tax T1–T3) | api | A fixture with card `t_7ae94ff5`'s real buckets gives the highest-tax-first result (about ₹70.9k vs ₹81.6k today) |
| 3 | spec-113 §2 (one-row transfers, wording) | api, web | Feed tests; screenshots of Money at 390 / 820 / 1440 px |
| 4 | spec-113 §3 (Today) | api (if needed), web | Screenshots of Today at 390 / 820 / 1440 px |
| 5 | spec-113 §4 (Money, overall limit, KPI off, nav, redirects) | api, web | Route-resolution test; screenshots at phone and iPad widths |
| 6 | spec-113 §5 (Investments, tax UI, ELSS) | api, web | Screenshots of the Tax & dates tab at iPad width; no clipped tables |

**At the start of every step:**
- rebase both feature branches on `origin/main` (`git fetch && git rebase origin/main`)
- re-run the step's tests
- after any migration, check that `alembic heads` shows exactly one head. Migration numbers on the branch
  must follow `main`'s latest, so renumber after a rebase if `main` moved.

**At the end of every step:**
- `pre-commit run --all-files` green in each touched repo
- API: full suite with `--cov-fail-under=80`
- Web: `npm test` + `npm run build` + lint
- report to the owner: commits, tests run with results, test ids removed or changed

Then stop and wait for the owner's "next".

**Prompt for the coding agent at each step** (owner pastes it):
> Do step N of `lifestack-api/docs/specs/spec-113-product-shape-roadmap.md` §7.2, following the named spec
> exactly.
> - Work only in the worktrees under `/home/sajan/lifestack-wt/reshape/` (create them per §7.1 if missing),
>   on branch `feat/lifestack-reshape`.
> - TDD; commit as you go with the step tag in each message.
> - Don't push, open PRs or deploy.
> - Finish with the step's local check and the end-of-step gates, then report.

### 7.3 Finish (after step 6, when the owner says "ship")

Order matters, because Cloudflare Pages deploys the web app as soon as web `main` changes. Web must
**never** go live before the API it calls.

1. **E2E:** in the `lifestack-e2e` worktree, write the E2E specs listed in each step's spec. Replace the
   removed selectors (§4.5). Run the **full** suite locally against the reshape stack.
2. **API PR:**
   - rebase on `origin/main` and rerun the full suite
   - push; open **one** PR `feat/lifestack-reshape`, whose description has one section per step (commits,
     tests, migrations) and card `t_7ae94ff5`'s tax evidence
   - CI green → merge with `--merge` when the owner says so
3. **Deploy the API** (`./scripts/deploy-production.sh` on the VM, per the `lifestack` skill) when the owner
   says so:
   - check that `/health` returns 200 and the migrations ran
   - this is when the new morning-push behaviour goes live, and the Saturday push starts on the next
     Saturday
4. **Web PR:** rebase, `npm test` + build, push, open **one** PR, CI green → merge (this auto-deploys).
   Check the live site on phone and iPad: installed PWA, old routes redirect.
5. **E2E PR:** push and open it. Its CI checks out the now-merged API and web `main`. Merge when green.
6. **Personal's pending items:** tell `personal` (one kanban card) to carry out the "Pending" section of its
   `portfolio/README.md`, now that §5 is live.

**Risks of the single-PR approach, and how they're handled:**
- **A large review.** Handled by per-step commit tags and per-step sections in the PR description.
- **Drift from `main` over weeks.** Handled by rebasing at every step start and renumbering migrations.
- **Production feedback** on the push and the tax figure arrives only after the final deploy. The local
  checks in 7.2 stand in for it.
- **If one feature misbehaves after the deploy,** revert its tagged commits in a follow-up PR. No commit
  mixes two steps.

## 8. Cross-cutting pitfalls
1. **PWA cache:** installed apps on phone and iPad can serve an old shell after route moves. Verify on a
   production build on both devices; the redirects cover stale links.
2. **Prod memory:** Today = cached dashboard summary + period change (cheap reads). Never load Investments
   analytics outside Investments.
3. **Old routes in stored notifications** keep working forever (4.4).
4. **Money correctness:** only spec-114 (tax), spec-115 (budget meaning) and 4.2 (overall limit) change
   what a number means. Everything else is layout and wording. Any other number change is a bug.
5. **Release order:** API PR → API deploy → web PR (auto-deploys) → E2E PR (§7.3). A green E2E PR run is
   only `@smoke`; run the full suite locally before opening it.

## 9. Acceptance (whole programme)
- 5 nav items; phone bottom bar with nothing clipped; iPad rail with 5 icons.
- One answer to "am I on budget?" everywhere; one tax figure, one LRS figure and one harvest list, all live.
- No morning push leads with a spending spike; the Saturday push shows the week with new money split out.
- Today shows net worth with the Week switch and "Needs you" / "All clear" (never both) without scrolling
  at 390 px.
- Money ▸ Activity shows transactions on the first screen; one row per transfer.
- Every API-emitted route resolves to a real page.
