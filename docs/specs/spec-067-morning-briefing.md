# Spec-067: Morning Briefing (deterministic, source-linked daily briefing)

**Created:** 2026-07-08
**Status:** Draft — owner review required before implementation
**Scope:** multi-repo, user-facing — `lifestack-api` (composed read endpoint + push job) and `lifestack-web` (briefing surface on the Dashboard). Two PRs, api first.
**Depends on:** spec-058 (dashboard insights), spec-052 (web push), spec-065 (net-worth snapshots), spec-064 (category-group budgets), weekly summaries (spec-016 lineage), budget guardrails. Product direction: `docs/product/PRODUCT_STRATEGY_AND_ROADMAP.md` §1 (Flagship Future Workflow — this briefing is its deterministic, finance-and-tasks v1) and §4 Immediate Focus item 6 — owner-accepted (see the roadmap's 2026-07-08 changelog entry).

---

## Problem

The roadmap's flagship workflow is a "calm operating briefing" each morning, and the Reviewer Demo Journey's step 1 is "open the dashboard and show the operating briefing." But the Dashboard is a readout, not a briefing: metric cards without prioritization, alert cues that don't say what to do first, and no ordered "here's your day" anywhere. Meanwhile every ingredient already exists server-side as a read model:

- dashboard insights (spec-058, `app/application/insights.py` — Notification rows, category `insight`)
- budget guardrails + overspend detection (`app/application/workflows.py`, `DashboardSummaryWorkflow` budget spotlight)
- overdue / due-today todos (`TodoService.get_summary_counts`, `get_next_due_items`)
- recurring transactions and recurring todos with next-due dates
- net-worth snapshots + valuation status (spec-065, `GET /finance/net-worth` + history)
- weekly summaries (`app/summaries/`)
- push delivery (spec-052, `app/notifications/`)

What is missing is purely compositional: one ordered, severity-ranked list — "2 overdue tasks, grocery budget at 92%, portfolio −0.8%, salary transfer due tomorrow" — where **every line cites and deep-links its source record**. This is the trust model in miniature (source-linked, deterministic, no black box) and the demo centerpiece.

## Goals

- A **deterministic** daily briefing: rules over existing read models, zero LLM involvement in v1.
- Every line carries: severity, one sentence of text, and a source link (record type + public id + route).
- Deterministic ordering (defined below) — same inputs, same briefing, every time.
- A designed empty state ("All clear — nothing needs your attention today"), not an accidental blank.
- Optional morning push reusing spec-052 delivery and existing notification preferences.
- One composed server-side read endpoint; the web renders, it does not compose.

## Non-goals

- AI/coach phrasing, LLM summarization, or personalization — v1 is rules only.
- Health data lines (arrives with Health Memory v1, which slots in as new line types).
- A user-configurable rule engine (thresholds are fixed constants in v1).
- Email delivery.
- Any schema migration or data change. **Retroactivity: N/A** — read-only composition; the push job writes only ordinary `Notification` rows.

## Solution

### A. Composition (lifestack-api)

A new `MorningBriefingWorkflow` in `app/application/workflows.py`, following the `DashboardSummaryWorkflow` pattern (constructor-injected services, per-section try/except so one failing domain degrades to omission rather than a 500).

**v1 line types, their sources, and severities:**

| # | Line type | Source read model | Severity rule |
|---|---|---|---|
| 1 | Overdue todos | `TodoService` overdue count + top items | `critical` if any overdue; one line summarizing count, top item named |
| 2 | Due-today todos | `TodoService` next-due items filtered to today | `warning` |
| 3 | Budget guardrail breaches | budget spotlight (`DashboardSummaryWorkflow` logic, extracted for reuse) | `critical` ≥ 100% utilization; `warning` ≥ 85% |
| 4 | Recurring transactions/todos due today or tomorrow | recurring rules' next-due dates | `info` |
| 5 | Net-worth daily change + valuation status | spec-065 snapshots (today vs previous) + live valuation status | `info`; `warning` when valuation status is degraded (`partial` / `conversion_required`) |
| 6 | Pending review work | imports awaiting commit; holdings verifications outstanding | `warning` |
| 7 | Latest weekly summary | `app/summaries/` most recent | `info`, only within 48h of generation — a fresh-summary pointer, not a permanent fixture. Summaries generate Monday 01:30 UTC (`weekly_summary` cron), so this line appears in Monday's and Tuesday's briefings, never on Sunday |
| 8 | Fresh insights | unread spec-058 `insight` notifications (≤ 48h old) | inherits the insight's own severity |

**Line shape (API contract):**

```json
{
  "severity": "critical | warning | info",
  "text": "Grocery budget at 92% with 12 days left",
  "source": {"entity_type": "budget", "entity_public_id": "uuid|null", "route": "/spending?tab=budgets"}
}
```

`route` is a client-routable path; `entity_public_id` is null for aggregate lines (e.g. "3 overdue todos"). Text is composed server-side from the same formatting rules the dashboard already uses (reporting currency, ISO dates) — the client renders it verbatim, which keeps web and push wording identical.

**Ordering rule (deterministic):** severity rank (`critical` > `warning` > `info`), then the table order above as a fixed domain tiebreak, then source public_id for total stability. Cap: 10 lines; overflow collapses into a final "and N more…" line linking to the relevant page.

**Empty day:** `lines: []` plus `all_clear: true`; the endpoint always also returns `generated_at` and `reporting_currency`.

### B. API surface

`GET /v1/dashboard/briefing` → `{generated_at, all_clear, lines: [...]}` — served by the dashboard router (it is the dashboard's evolution, not a new module), backed by `MorningBriefingWorkflow`.

**Server-side composition, justified:** (1) the push job needs the identical composition server-side anyway — client-side assembly would mean two implementations of the same rules that WILL drift (the exact drift class spec-065 just eliminated for cash); (2) severity/ordering is product logic, testable once with pytest rather than re-tested in vitest; (3) one round-trip on the page reviewers see first. This follows the existing `DashboardSummaryWorkflow` precedent.

### C. Morning push (lifestack-api)

- A `morning_briefing_job` run via `run_workspace_job` (session-level advisory lock, skip-if-held, locked/skipped-count logging — same discipline as `budget_guardrails_job`).
- Per workspace: compose the briefing; if not `all_clear`, write ONE `Notification` (category `briefing`, severity = max line severity, title "Morning briefing", body = top 3 line texts, entity route `/`). Existing `NotificationService.notify` preference-gating applies; spec-052 push delivery picks it up. All-clear days send nothing (calm by default).
- **Default (owner decision, 2026-07-08): ON for users with at least one active push subscription, OFF for everyone else.** Implemented via the existing per-category notification-preference row (`briefing`) — no schema change (`category` is a free string with a unique constraint per user/workspace). Absence of a `briefing` preference row counts as enabled when the user has an active push subscription; an explicit row always wins, and the existing notification-preferences UI is the off switch.
- Schedule: daily at 02:30 UTC (≈ 08:00 IST) via the existing APScheduler registration in `app/main.py`, env-overridable (`BRIEFING_JOB_HOUR_UTC`/`MINUTE`). This deliberately runs after Monday's 01:30 UTC `weekly_summary` cron, so a fresh weekly summary lands in that same Monday briefing. Registration must be verified wired (the spec-065 job famously missed this).

### D. Briefing surface (lifestack-web)

**Proposal: the briefing TOPS the Dashboard rather than replacing it** (owner decides at this spec's review). Justification: the 2026-07 UX hardening pass just made the dashboard cards useful launchpads (deep-linked metrics, insights, "data as of" line); the briefing is the ordered narrative layer those cards lack, not a substitute for at-a-glance numbers. Full replacement would also orphan the first-run "Get started" checklist placement. If living together proves cluttered, replacement becomes a cheap follow-up — the reverse migration is not.

- A "Today" briefing card at the top of `DashboardPage` (below the first-run "Get started" checklist while that shows): severity-dotted ordered lines, each a link to its `route`; all-clear state with a distinct calm design; skeleton while loading (existing `FeedbackStates`).
- New query key + Zod-parsed service + type per house conventions.
- Notifications page: `briefing` added to the existing category label map ("Morning briefing").

**Migration notes for DashboardPage:** the metric cards, insight section, and "data as of" line stay; the briefing card is inserted above them. No existing dashboard endpoint changes. If the owner chooses replacement instead, the cards' data needs survive via the existing summary endpoint on a secondary view — scoped then, not now.

## Test plan

- **api unit (Red first):** one test module per line type — inclusion threshold, severity assignment, text formatting; ordering (severity → domain → id) and the 10-line cap; empty-day → `all_clear`; per-section failure degrades to omission (patterned on `DashboardSummaryWorkflow` tests).
- **api integration:** `GET /v1/dashboard/briefing` against seeded fixtures (workspace isolation, RBAC member+); `morning_briefing_job` — writes one notification with correct category/severity, skips all-clear workspaces, second concurrent invocation skips on the advisory lock (existing locked-job test pattern); default matrix — push-subscribed user with no `briefing` preference row gets the notification, unsubscribed user with no row does not, an explicit off row always wins.
- **web (Red first):** briefing card renders lines with severity + working links; all-clear state; loading skeleton; service Zod parse. Gates: vitest ≥ 70, `npm run build`, lint.
- **e2e:** one `briefing.spec.ts` journey — seed an overdue todo + an overspent budget, load the dashboard, assert ordered briefing lines and that clicking a line lands on the resolving page; plus an all-clear assertion on a clean workspace.

## Rollout

Two PRs: api (workflow + endpoint + job + tests, `docs/JOBS.md` updated in the same pass per house rule), then web. No feature flag — the endpoint is additive and the card degrades to nothing on error. Push defaults on only for already-push-subscribed users (owner decision below); everyone else opts in via notification preferences.

## Owner decisions (2026-07-08)

- **Morning-push default: ON for users with an active push subscription, OFF otherwise.** Subscribing to push already expressed intent to be notified; users who never subscribed get nothing. The existing preferences UI is the off switch.
- **Thresholds confirmed:** budget warning at 85% utilization, fresh-insights window at 48h. A configurable rule engine stays a non-goal.

## Open questions for the owner

1. **Briefing tops vs. replaces the Dashboard** — spec proposes TOPS (justified in §D); decide at this spec's review.
