# Product Strategy and Roadmap

Date: 2026-06-03
Last updated: 2026-07-12

Scope: current product positioning, implementation status, post-Gate 0 backlog, and staged roadmap for mobile, health tracking, medication reminders, workout tracking, document intelligence, second brain, and personal coach workflows.

## Changelog

- **2026-07-12 — Immediate Focus backlog fully cleared; doc caught up to specs 066–074.** Every item in the 2026-07-08 "Immediate Focus" list is now done: e2e CI gating (lifestack-e2e, merged 2026-07-09), demo-path UX hardening P0/P1/P2 (web#90/#94/#95), capture consolidation (spec-066), and Morning Briefing (spec-067). Since then, specs 068–074 also shipped: todo subtasks/organization, **Health Memory V1** (Track 1's first slice — medications + weight, spec-069, ahead of its original long-term sequencing), export completeness/round-trip, dividend income tracking + historical FX/net-worth ingestion + investment return metrics, and consolidation of three bespoke bulk-paste flows into the shared imports framework. A companion full spec-vs-code audit (2026-07-12) also found and fixed 8 stale status headers across specs 033/048/070/073/074 (api) and 003/004/007 (web) — see those specs' own status lines; no roadmap-relevant surprises among them. **No item is currently queued as "next"** — see §4 for the candidate backlog and the owner-sequencing note.
- **2026-07-08 — external product assessment folded in (owner-accepted).** An external product review (`PRODUCT-ASSESSMENT.md` + `UX-REVIEW.md`, dated 2026-07-08) proposed four rethinks; the owner accepted all four. Changes in this revision: Immediate Focus now includes demo-path UX hardening and capture consolidation at parity with CI gating, plus Morning Briefing as the next major product slice; Voice/capture promoted from Experimental to Secondary; Track 1 (Health Memory) re-sequenced ahead of Track 2 (Mobile Companion, now shrunk to camera upload + native health-app sync); finance correctness campaign (specs 040–065) declared V1-complete.

## 1) Product Thesis

Lifestack should evolve from a personal productivity and finance system into a private personal coach powered by structured life data.

The product should not become a generic chatbot with loose files attached. The durable differentiator is a shared data model where actions, money, health, documents, preferences, and memory can be captured, reviewed, exported, and reasoned over with clear source trails.

Lifestack is primarily a personal project and personal operating system. Monetization and SaaS expansion should remain secondary unless they clearly preserve the personal vision instead of bending the product toward broad-market compromise.

### Why Now

People already produce enough personal data to make better daily decisions: tasks, transactions, investments, sleep, weight, workouts, medication schedules, reports, receipts, notes, and documents. The problem is fragmentation. Most of that data lives in disconnected apps, inboxes, files, and health platforms.

Lifestack's opportunity is to become the private system that turns this fragmented data into useful personal context without making the user surrender ownership or accept a black-box assistant.

### Flagship Future Workflow

The long-term product should make this kind of morning review possible:

- today's tasks and overdue follow-ups
- medication reminders and adherence check-ins
- sleep, weight, and workout context
- spending pressure and investment watch items
- important document or health-report follow-ups
- a coach summary that cites the source data behind each recommendation

That workflow should feel like a calm operating briefing, not a chat transcript or a pile of dashboards.

## 2) Current Position

The current product already has a strong foundation:

- Auth, workspace scoping, todos, spending, investing, dashboard, notifications, summaries, imports, exports, recurring workflows, and the Phase 1 voice/tool-calling capture surface.
- A scheduler and notification model that can later support medication reminders and health follow-ups.
- A service-layer architecture that gives future AI/mobile/document adapters a stable place to call into existing product capabilities.

The main risk is sequencing. Health, documents, and memory are high-trust domains. They should be planned now but implemented only after the product has stronger reliability, security, mobile ergonomics, and E2E coverage.

### Current Product Definition

Lifestack today should be positioned as a **finance-led personal operations command center**.

The strongest current product story is not "everything tracker" and not "AI assistant." The product is most coherent when it helps a user answer:

- What is happening with my money?
- What spending pressure or budget risk needs attention?
- What is my portfolio worth across accounts and currencies?
- What recurring life-admin work is coming up?
- What imported or captured data needs review?
- What should I act on next?

This gives the current application a clear center of gravity while still leaving room for health, documents, memory, and coaching later.

### Current Product Surface Priority

| Surface | Product Role | Priority |
|---|---|---|
| Dashboard | Operating briefing across money, tasks, summaries, and alerts. | Primary |
| Spending | Core finance workflow for transactions, budgets, recurring rules, and guardrails. | Primary |
| Investing | Portfolio context, FX-aware valuation, holdings, cash, performance, and ETF/MF look-through analytics. | Primary |
| Imports and exports | Trust, portability, and realistic data movement. | Secondary |
| Todos and notifications | Action layer for reminders, follow-ups, and system-generated work. | Secondary |
| Workspaces and RBAC | Trust boundary and demo credibility layer. | Supporting |
| Master config | Admin/settings surface, not a main destination. | Supporting |
| Voice/capture | Universal input layer; text-first with voice mode. | Secondary |
| Health | Manual health tracking — medications (schedule, reminders, adherence) and weight (log, trend). V1 slice (spec-069); expands per Track 1 backlog in §6. | Secondary |

### Reviewer Demo Journey

The first five minutes of a public portfolio demo should be intentionally guided:

1. Open the dashboard and show the operating briefing: financial health, upcoming tasks, latest summary, and portfolio snapshot.
2. Open spending and show budget guardrails plus recurring transactions.
3. Open imports and show review-before-commit behavior for realistic data ingestion.
4. Open investing and show account-backed holdings, cash, FX conversion, and performance context.
5. Open workspace/admin briefly to show RBAC, active workspace context, and safe demo reset.
6. Point to E2E, docs, and security posture as evidence that the product is engineered, not just assembled.

The demo should make insight more visible than data entry. Reviewers should leave with the impression that Lifestack helps the user notice, decide, and act.

## 3) Implementation Status

This roadmap is the living home for product sequencing. Specs remain the source of truth for scoped implementation contracts or historical closure records.

| Status | Specs | Roadmap Meaning |
|---|---|---|
| Historical foundation | [`spec-pack-v1-plan.md`](../specs/spec-pack-v1-plan.md), [`spec-029-current-product-demo-readiness-roadmap.md`](../specs/spec-029-current-product-demo-readiness-roadmap.md) | Gate 0 and the V1 spec pack are complete historical records, not active backlog. |
| Implemented platform baseline | Specs 001, 002, 004, 005, 006, 007, 008, and 010 | API conventions, workspace isolation, audit logging, scheduler foundation, exports, dashboard reads, investing MVP, and FastTodo reference decisions are implemented baseline capabilities. |
| Implemented finance and investing baseline | Specs 011, 012, 014, 022, 023, 027, [`031`](../specs/spec-031-automated-price-updates-and-ui.md), [`033`](../specs/spec-033-hybrid-instrument-catalog.md), and [`034`](../specs/spec-034-constituent-csv-import.md) | Currency governance, look-through analytics, performance V1, account identity, price refresh, hybrid global instrument catalog, and workspace-facing constituent CSV import are implemented. The unreliable automated constituent path from retired Spec 032 is no longer part of the runtime. |
| Implemented workflow and operations baseline | Specs 003, 009, 013, 015, 016, 017, 019, 020, 024, 025, 026, and 028 | Spending, scheduler workflows, recurring todos, notifications, weekly summaries, CSV imports, runtime integration, audit remediation, and Gate 0 hardening are implemented at their documented stage. |
| Implemented spending analytics and ledger | feat/spending-analytics branch | Category breakdown, budget-vs-actual, and savings-rate analytics are implemented. Per-account transaction ledger with running balance (GET /spending/accounts/{id}/ledger) and derived wallet cash-balance coupling (GET /finance/accounts/{id}/balance) are implemented and covered by integration tests. |
| Implemented imports enhancements | feat/spending-analytics branch | Async/background import workers, `.xlsx` streaming, large-file handling, and import preview rows are implemented. |
| Archived or partial capture path | [`Spec 018`](../specs/spec-018-quick-capture.md), [`Spec 021`](../specs/spec-021-voice-agent-function-calling.md) | Spec 018 quick capture is archived/deferred to roadmap. Spec 021 voice/tool calling is implemented as Phase 1; production capture expansion remains a roadmap item. |
| Proposed/current slices | [`Spec 030`](../specs/spec-030-cli-management-commands.md) | CLI management commands remain a proposed/current implementation candidate. |
| Deferred future data model | [`Spec 035`](../specs/spec-035-platform-market-data-curation.md) | Platform market-data curation is a deferred backlog item until the workspace-scoped investing flows, permission model, and provenance requirements are ready. |
| Implemented 2026-06→07 wave | Specs 036–061 and [`063`](../specs/spec-063-cdsl-demat-cas-support.md) | Cash-correctness hardening (040–050), corporate actions/splits ([`051`](../specs/spec-051-corporate-actions-splits.md)), web push (052), CAS PDF imports ([`056`](../specs/spec-056-cams-cas-pdf-import.md) CAMS, [`060`](../specs/spec-060-demat-cas-holdings-verification.md) NSDL verification, 063 CDSL), NSE bhavcopy price feed ([`057`](../specs/spec-057-nse-bhavcopy-price-feed.md)), dashboard insights ([`058`](../specs/spec-058-dashboard-insights.md)), and voice usability (059, 061) are implemented and merged. |
| Implemented 2026-07-08 wave | [`062`](../specs/spec-062-category-delete-and-merge.md), [`064`](../specs/spec-064-category-group-budgets.md), [`065`](../specs/spec-065-net-worth-over-time.md) | Category delete/merge and category-group recurring budgets (api#131, web#86), and net-worth-over-time daily snapshots + history graph (api#132, web#87) are implemented and merged. spec-065's snapshot job was fixed pre-merge to compute live via `InvestingSummaryService` instead of depending on the on-demand `portfolio_snapshots` table, and its APScheduler registration was added post-merge. |
| Implemented 2026-07-08→11 wave | [`066`](../specs/spec-066-capture-consolidation.md), [`067`](../specs/spec-067-morning-briefing.md), [`068`](../specs/spec-068-todo-organization.md), [`069`](../specs/spec-069-health-memory-v1.md), [`070`](../specs/spec-070-export-completeness-and-roundtrip.md), [`071`](../specs/spec-071-investment-return-metrics.md), [`072`](../specs/spec-072-historical-data-ingestion.md), [`073`](../specs/spec-073-dividend-income-tracking.md), [`074`](../specs/spec-074-consolidate-bulk-paste-imports.md) | Capture consolidation (api#137/139, web#91); deterministic morning briefing (api#138/140, web#93); todo subtasks + Clear completed (api#144, web#102); **Health Memory V1** — medications + weight, Track 1's first slice (api#145, web#103); export completeness across finance/health/orders (api#148, web#107); investment return metrics + historical FX/net-worth ingestion + dividend income tracking (api#150, web#110); consolidation of the dividend/FX/net-worth bulk-paste flows into the shared imports framework (api#151, web#111). All implemented and merged; e2e suite gated in CI in the same window (lifestack-e2e, merged 2026-07-09) and demo-path UX hardening P0/P1/P2 landed (web#90, #94, #95). |
## 4) Near-Term Roadmap

This is the Post-Gate 0 roadmap backlog, promoted near the top because it contains the next practical product slices. These items should deepen the current finance-led product before Lifestack expands into new life domains.

### Immediate Focus

**2026-07-12 — this entire list is now cleared.** All seven items below are done. No replacement "Immediate Focus" list has been decided — see the candidate backlog below the list, and treat sequencing among those as an open owner decision, not something this doc infers on its own.

1. ~~Merge api#130, then implement spec-065 net-worth-over-time~~ — done 2026-07-08 (api#132, web#87). Daily history now accumulates via the `net_worth_snapshot` scheduler job (07:00 UTC) plus an opportunistic upsert on every `GET /finance/net-worth` read.
2. ~~Spending model wave: spec-062 category delete & merge, then spec-064 recurring date-ranged budgets + category groups~~ — done 2026-07-08 (api#131, web#86).
3. ~~Gate the e2e suite in CI~~ — done 2026-07-09 (lifestack-e2e#20): smoke-tagged subset runs on every PR, full suite on push to main and nightly cron (03:00 UTC). Cross-repo dispatch from api/web merges remains an unwired, documented gap — the nightly cron is the backstop.
4. ~~Demo-path UX hardening~~ — done: UX-REVIEW.md P0/P1 (web#90) and P2 (web#94, #95) batches all merged 2026-07-08/09.
5. ~~Capture consolidation~~ — done 2026-07-08 (spec-066, api#137/139, web#91): one tool-response contract, structured confirmation cards, single entry point.
6. ~~Morning Briefing~~ — done 2026-07-08 (spec-067, api#138/140, web#93): deterministic, source-linked composition of dashboard insights, weekly summaries, budget guardrails, overdue todos, and net-worth snapshots, zero LLM involvement in v1, dashboard card + push delivery.
7. ~~Keep the standing deferrals on spec-033 and platform-wide market-data curation~~ — spec-033's deferral premise was already stale when this line was written: the hybrid instrument catalog had shipped 2026-06-24 (migration `0032_hybrid_instrument_catalog.py`), corrected 2026-07-12. [`Spec 035`](../specs/spec-035-platform-market-data-curation.md) platform-wide curation remains genuinely deferred — no admin persona/provenance/rollback model yet, still correctly out of scope.

**Candidate backlog (unsequenced — pick with the owner, not from this doc alone):**

- **Core Product Depth** (table below): JWT library migration off `python-jose`, notification delivery channels beyond push (email digest), voice/capture production hardening, wallet-reconciliation UX, custom financial KPIs, remaining currency-display polish.
- **§6 Long-Term Product Sequence:** Track 1 (Health Memory) shipped only its V1 slice — medications + weight (spec-069); sleep, workouts, vitals, labs, and symptoms are explicitly out of scope for v1 (spec-069 non-goals) and are the natural next slice within the same track before moving to Track 2 (Mobile Companion). Tracks 3–7 (Health Sync, Document Intelligence, Second Brain/RAG, Agent Access, Personal Coach) are unstarted.

### Core Product Depth

| Area | Roadmap Item | Why It Belongs Here |
|---|---|---|
| Spending analytics (Implemented) | Category breakdown, budget-vs-actual, and savings-rate analytics. | Implemented under feat/spending-analytics branch. |
| Spending ledger (Implemented) | Per-account transaction ledger with running balance, opening/closing page balances, debit/credit color coding, and workspace isolation. Backend: `GET /spending/accounts/{id}/ledger`. Frontend: Ledger tab on Spending page. | Implemented under feat/spending-analytics branch. |
| Wallet/account balance coupling (Implemented) | Derived spending balance for each account: `GET /finance/accounts/{id}/balance` computes income minus expenses from transaction history. Surfaced as a balance summary card on the Ledger tab. Deliberately independent from investing cash balances. | Implemented under feat/spending-analytics branch. |
| Imports (Implemented) | Async/background import workers, `.xlsx` imports, smart column mapping, and large-file streaming. | Implemented under feat/spending-analytics branch. |
| Daily-work surface polish (Implemented) | Overdue todo indicators, recurring status badges, and dashboard cues. | Implemented under feat/spending-analytics branch. |
| Notifications | Email delivery, push delivery, real-time notification transport, grouping, and digest variants. | Delivery channels depend on mobile/email infrastructure and should be sequenced with notification strategy. |
| Currency display | Remaining frontend-wide display polish, locale/date/number profiles, and historical FX replay for every view. | These are consistency and polish tracks after the implemented finance settings foundation. |
| Voice/capture | WebRTC-grade production transport, broader capture domains, multi-item capture, AI-assisted routing, and ADK migration planning. | Capture is useful as an input layer; Google ADK evaluation and voice-first migration guide is documented in [spec-039-adk-evaluation-and-migration-guide.md](../specs/spec-039-adk-evaluation-and-migration-guide.md) for Phase 2. |
| Weekly summaries | Configurable summary cadence, regeneration/admin correction flows, and expanded insight surfaces. | These are workflow-product improvements, not changes to the implemented weekly-summary contract. |
| Custom financial KPIs | User-defined budget/spend KPIs beyond the implemented category and category-group budgets. | Parked for intentional design; not scoped by spec-064. |
| Wallet ledger (reconciliation) | Statement matching, multi-account reconciliation view, and richer transfer timeline UX. | Deeper finance-product work building on the implemented ledger foundation. |
| JWT library maintenance | Migrate from `python-jose` to `PyJWT` or `joserfc`. | python-jose is no longer actively maintained; planning migration mitigates dependency security risk. |

### Investing and Market Data

**2026-07-08 — Finance depth: V1 complete.** The cash-correctness and investing-accuracy campaign (specs 040–065: transfer-inclusive reconciliation, FIFO cost basis, corporate actions, account-currency invariant, CAMS/NSDL/CDSL CAS imports, bhavcopy pricing, net-worth history) is done. New finance specs now require explicit justification against briefing, health, and capture priorities rather than being the default next slice — each additional India-specific ingestion or reconciliation spec has diminishing reviewer-visible value (see `PRODUCT-ASSESSMENT.md` Rethink 4).

| Area | Roadmap Item | Why It Belongs Here |
|---|---|---|
| Investing performance | Richer return math, deeper visualization, benchmark comparison, dividend/total-return views, and scheduled/background price-refresh cadence. | On-demand automated price refresh is implemented; deeper performance analytics and scheduled pricing should be scoped as explicit product slices. |
| Investing summary valuation | Query latest price data from `HoldingPrice` table instead of using cost basis. | Resolves misleading investing overview totals when asset values fluctuate. |
| Hybrid instrument catalog | **Implemented** ([`Spec 033`](../specs/spec-033-hybrid-instrument-catalog.md), migration `0032_hybrid_instrument_catalog.py`, 2026-06-24 — corrected 2026-07-12, doc previously mismarked deferred): global public instruments/companies with workspace-scoped tenant overrides. | Reduces duplicate public securities and redundant provider calls; global-first resolution is live in `app/investing/service.py`. |
| Look-through analytics | UX alerts, quality scoring, company identity normalization, derivative look-through, and deeper constituent-provider coverage. | Look-through analytics and automated ETF/MF constituent ingestion are implemented; these are advanced accuracy, scale, and UX tracks after V1 correctness. |
| Corporate actions (stock splits) | **Implemented ([`spec-051`](../specs/spec-051-corporate-actions-splits.md), api#102, merged 2026-07-04):** splits, reverse splits, and bonus issues are first-class replayed events, golden-tested. | Closes the un-applied-split risk (understated share counts, distorted FIFO cost basis, e.g. NVDA 10:1, GOOGL 20:1 in imported IND Money data); manual order edits are no longer the workaround. |
| Brokerage in cost basis | **Deprioritized (owner decision, 2026-07-07): not a tracked roadmap item.** Historical fee backfill for imported orders and the related residual reconciliation drift on pre-spec-049/050 data are the owner's own historical-data quirk, not a systemic app defect — hand-correct via manual order edits if/when it matters, rather than building a backfill feature. | Fees are stored per order but excluded from cost basis, so invested is under-reported and unrealized gain over-reported by the fee total (~$44 across imported IND Money orders); imported GROWW orders carry $0 fees. Left as owner-editable rather than spec'd. |
| Platform market data | [`Spec 035`](../specs/spec-035-platform-market-data-curation.md): platform-admin curation for shared/global constituent datasets, instrument prices, licensed market-data uploads, provenance, and rollback. | Workspace-level constituent CSV import is handled by [`Spec 034`](../specs/spec-034-constituent-csv-import.md); shared/global market-data curation is a later-stage permission and data-governance problem. |

## 5) Long-Term Product Direction

Recommended direction: keep the long-term product track, but gate implementation.

### Why This Is Worth Building

- Health data makes Lifestack more useful as a daily personal operating system, not just a task and finance tracker.
- Medication and health reminders reuse existing product strengths: scheduler, notifications, todos, dashboard, and weekly summaries.
- Documents create source-backed memory and reduce manual entry for receipts, statements, prescriptions, reports, and forms.
- A personal coach becomes valuable only when it can cite structured personal context instead of relying on generic chat history.

### Why It Should Not Be The Immediate Next Branch

- Health and document data raise the privacy and safety bar.
- Mobile sync is a prerequisite for low-friction health tracking.
- Document RAG without source discipline can create false confidence.
- Broad coaching before reliable data ingestion would feel impressive but fragile.

### Trust and Safety Boundaries

Lifestack can support reflection, reminders, planning, summaries, and source-backed recommendations. It should not diagnose medical conditions, prescribe medication, provide professional financial advice, or take sensitive health/financial actions autonomously.

The coach should default to user-confirmed actions: create a reminder, open a review task, summarize a trend, or ask the user to verify source data before anything important changes.

### Interface Boundaries

Chat and voice should be input and review interfaces over the product, not the product itself. The app should remain useful through direct UI workflows, dashboards, capture surfaces, notifications, and exports even when no assistant is active.

Settings and master configuration should remain deliberately low-traffic. They are useful for reducing clutter across feature pages, but they should feel like a lean utility area the user visits rarely, not a major product destination.

MCP should be treated as a later-stage integration layer with real product value. The goal is to let trusted external agents connect to the user's Lifestack context, preferences, second brain, and selected domain data through explicit permissions. This makes migration between AI assistants smoother because the user's durable personal context lives in Lifestack rather than inside any one model vendor.

## 6) Long-Term Product Sequence

The roadmap can use stage numbers for planning, but user-facing product eras should have memorable names:

**2026-07-08 — re-sequenced (owner-accepted):** Health Memory now precedes Mobile Companion. Web push (spec-052) and an installable PWA (web spec-005) already ship, so manual health tracking (weight, medications) has no mobile dependency; Mobile Companion is shrunk to the items that are genuinely mobile-only (camera upload, native health-app sync) and follows once those are worth building. Health Sync still follows Health Memory, unchanged.

| Era | Product Name | Purpose |
|---|---|---|
| Gate 0 | Foundation | Make the current product secure, reliable, demoable, and honest in docs. |
| Track 1 | Health Memory | Add health metrics, medications, workouts, and source-aware longitudinal records — manual, web/PWA-first. |
| Track 2 | Mobile Companion | Camera upload for documents and native health-app sync, once a native wrapper earns its cost. |
| Track 3 | Health Sync | Reduce manual logging through mobile health-app integrations. |
| Track 4 | Document Intelligence | Turn documents into source-linked structured records. |
| Track 5 | Source-Backed Second Brain | Connect documents, notes, records, and activity through cited retrieval. |
| Track 6 | Agent Access | Expose selected Lifestack context through MCP and other permissioned integration surfaces. |
| Track 7 | Personal Coach | Turn structured personal context into planning support and review workflows. |

### Gate 0: Product Foundation Stabilization

Goal: make the existing product credible, secure, and demoable before adding new life domains.

Status: complete. The historical closure record lives in [`spec-029-current-product-demo-readiness-roadmap.md`](../specs/spec-029-current-product-demo-readiness-roadmap.md).

Gate 0 is not an active roadmap anymore. New pending work should be tracked in this product roadmap, focused implementation specs, audit documents, or GitHub issues/PRs.

Closed acceptance criteria:

- Role-based authorization is enforced where roles already exist.
- Password policy, session controls, and security findings are addressed.
- One-command full-stack E2E path works from a clean checkout.
- Demo seed/reset flow exists, is explicitly demo-mode gated, and is restricted to owner/admin roles.
- Workspace selection keeps session refresh-token rotation consistent.
- The frontend has an explicit active-workspace model and destructive actions target that workspace only.
- Multi-currency investing summaries and performance snapshots use clear reporting-currency semantics.
- Specs, ERD, README, and roadmap documents distinguish implemented behavior from planned or partially implemented behavior.
- Mobile shell/navigation is responsive enough for lightweight capture and review flows.
- READMEs separate current features from planned roadmap scope.

### Track 1: Health Memory

**Status: V1 shipped 2026-07-09** ([`spec-069`](../specs/spec-069-health-memory-v1.md), api#145, web#103) — medications (recurrence-based dose schedules, push reminders, adherence log) and weight (quick log, trend chart, kg only) are live, with briefing/weekly-summary/export integration. **Explicitly out of scope for v1 (spec-069 non-goals), remaining open within this track:** sleep, workouts, vitals, labs, symptoms; any device/health-app sync or document extraction (Tracks 2–4); nested "course" medication schedules.

Goal: support manual health tracking before depending on external sync. Web/PWA-first — no mobile dependency, since installable PWA (web spec-005) and web push (spec-052) already ship.

Scope:

- Weight, sleep, workouts, vitals, symptoms, labs, and medications.
- Medication schedules, refill notes, adherence events, and reminders.
- Workout logs for strength/cardio sessions, recovery notes, and trend review.
- Health dashboard cards and weekly summary integration.
- Follow-up todos generated from health reminders and lab review needs.

Data rules:

- Every health record has source metadata: manual, mobile sync, document extraction, or import.
- Health records are exportable.
- The UI distinguishes measured, user-entered, and extracted values.

### Track 2: Mobile Companion Foundation

Goal: cover the parts of the capture and sync surface that genuinely require a phone. Shrunk from the original mobile-shell scope: quick capture, push notifications, and an installable app shell are already delivered on web (voice/capture endpoint, spec-052 web push, spec-005 PWA manifest) — rebuilding them natively is not in scope here.

Scope:

- Camera upload for documents.
- Health-app sync architecture for Apple Health, Google Health Connect, or equivalent providers.
- Background sync plumbing for the above.

Non-goals:

- Full health analytics.
- General personal coach automation.
- Multi-user SaaS mobile features.
- Rebuilding capture, push notifications, or an installable shell already shipped on web.

### Track 3: Health Sync

Goal: reduce manual logging for metrics that devices already collect.

Scope:

- Sleep, weight, workouts, steps/activity, heart-rate style metrics where supported by the mobile provider.
- Sync conflict policy for duplicate records.
- Source/provider freshness metadata.
- Import review screen for high-risk or ambiguous records.

Non-goals:

- Medical diagnosis.
- Provider-specific advanced analytics before the normalized model is stable.

### Track 4: Document Intelligence

Goal: turn documents into source-linked structured data.

Scope:

- Upload and storage lifecycle for receipts, statements, prescriptions, lab reports, and forms.
- Extraction pipeline with confidence scores.
- Human review before writing high-impact structured records.
- Links from normalized records back to source document spans or pages.
- Export/delete lifecycle controls.

Non-goals:

- Blind auto-write of sensitive extracted health or financial data.
- Large-scale document search before storage and deletion controls are strong.

### Track 5: Second Brain and RAG

Goal: provide retrieval over personal context with citations.

Scope:

- Journal, notes, documents, tasks, health records, finance events, and summaries in one retrieval layer.
- Timeline and context views across life domains.
- Source-backed answers that cite documents or normalized records.
- User controls for what data domains the assistant may access.

Non-goals:

- Using chat history as the canonical memory store.
- Uncited claims for sensitive domains.

### Track 6: MCP and Agent Access

Goal: make Lifestack the durable personal context source for trusted external AI agents.

Scope:

- Permissioned MCP tools over selected user data domains.
- Read access to preferences, second-brain memory, todos, documents, and normalized finance/health summaries where explicitly allowed.
- Investment context export for research agents: holdings, ETF look-through constituents, stock counts, FX context, overlap, and tax/reporting inputs.
- Audit trails showing which external agent accessed which domain and when.
- Capability-scoped writes only after the product has strong confirmation, logging, and rollback patterns.

Non-goals:

- Treating MCP as a user-facing module in the main navigation.
- Exposing raw private data to arbitrary agents without consent, scoping, and logging.
- Letting external agents make autonomous financial, health, or destructive data changes.

### Track 7: Personal Coach

Goal: help the user notice, decide, and act across life domains.

Scope:

- Coach summaries over tasks, spending, investing, health, workouts, medications, documents, and journal context.
- Recommendations that create review tasks or reminders rather than directly mutating sensitive data.
- Explainable plans with visible sources, assumptions, and confidence.
- Permission boundaries and audit logs for assistant actions.

Non-goals:

- Medical advice or diagnosis.
- Autonomous financial or health actions.
- Chat-first product design that bypasses structured workflows.

## 7) Personal Data Trust Model

Trust is a product feature, not only an implementation detail.

- **Consent:** users choose which domains the assistant, sync jobs, and retrieval layer may use.
- **Agent scoping:** MCP and external-agent access must be explicitly permissioned by domain and capability.
- **Source tracking:** every imported, synced, extracted, or assistant-used record keeps visible source metadata.
- **Review before high-impact writes:** sensitive health and financial data should require human confirmation before normalized records are created or changed.
- **Deletion and export:** health, document, memory, and coach-derived data must remain portable and removable.
- **Audit trails:** assistant actions and automated workflows should leave enough history for the user to understand what happened and why.
- **Cited responses:** document-backed and health-backed answers should cite source records instead of presenting unsupported conclusions.

## 8) Candidate Module Parking Lot

These modules make conceptual sense, but they are not current roadmap commitments. Keep them here for product memory and revisit only when they strengthen the daily briefing, second brain, or coach loop.

| Candidate | Priority Signal | Why It Could Fit | Caution |
|---|---|---|---|
| Calendar and Time | High | Tasks, reminders, medicine, routines, workouts, appointments, and planning all need time context. | Avoid building a full calendar competitor before capture and reminders are strong. |
| Goals, Habits, and Routines | High | Gives the coach a target: sleep, spending, workouts, medication adherence, focus, reading, or personal projects. | Avoid streak gamification that adds pressure without insight. |
| Projects | High | Connects tasks, documents, expenses, deadlines, notes, and summaries around outcomes. | Keep it lightweight; todos should not become enterprise project management. |
| People and Relationships | Medium | Personal CRM, family context, follow-up reminders, gift ideas, and shared-life memory could be meaningful. | Privacy and emotional sensitivity are high; start small if ever added. |
| Mood, Mental Health, and Energy | Medium-high | A small check-in can explain patterns across sleep, workload, workouts, spending, and routines. | Keep it supportive and reflective, not diagnostic or clinical. |
| Food and Nutrition | Medium | Useful if health tracking becomes serious; connects weight, workouts, sleep, energy, and labs. | Easy to become tedious; prefer lightweight capture before detailed macros. |

Mental health and energy should probably rank above food/nutrition for the personal-coach vision, especially if the first useful version is a low-friction check-in rather than a dense tracker.

## 9) Product Risks

| Risk | Why It Matters | Mitigation |
|---|---|---|
| Health privacy | Health data is high-trust and sensitive. | Ship export/delete, source metadata, and permission boundaries before broad sync. |
| False confidence from RAG | Retrieval can sound correct without being grounded. | Require citations and source links for document-backed answers. |
| Mobile dependency | Health sync needs mobile, but mobile can become a large project. | Start with mobile capture/notification shell before deep sync. |
| Scope explosion | Health, documents, and coach features can each become a full product. | Sequence tracks and use non-goals aggressively. |
| Coach safety | Recommendations can be mistaken for professional advice. | Keep coach as planning support with disclaimers, audit trails, and user-confirmed actions. |
| Agent access leakage | MCP can make private data easier to over-share. | Use explicit scopes, audit logs, and conservative defaults. |
| SaaS pressure | Monetization can distort the personal operating-system vision. | Keep SaaS later and optional; do not optimize early modules for broad-market admin needs. |

## 10) Documentation Alignment

The README and other public-facing docs should stay aligned with this roadmap:

- Describe the current product as a finance-led personal operations command center before introducing future health, document, and coach tracks.
- Include a short reviewer/demo journey for the first five minutes of product evaluation.
- Keep current implemented features separate from planned roadmap tracks.
- Weight tracking and medication reminders are implemented today (Health Memory V1, spec-069) and may be claimed as such; sleep, workouts, vitals, labs, health-app sync, documents/RAG, second brain, and personal coach remain planned future tracks — do not claim those as implemented.
- Name the stabilization gate before new high-trust modules.
- Describe AI as an interface over structured services, not the foundation.
- Explain the trust model and coach boundaries at a high level.
- Name the flagship morning-review workflow so the roadmap feels like a product journey, not a list of modules.
- Clarify MCP as a permissioned integration layer for trusted agents, not a main product module.
- Keep candidate modules as a parking lot rather than active roadmap promises.

## 11) Strategic Verdict

Proceed with the product track, but do not make it the immediate feature implementation branch.

The current product should be presented as a finance-led personal operations command center with investing, tasks, imports, exports, and workspace controls as supporting proof points. The correct next move is to stabilize that foundation, make the demo path safe and crisp, then introduce mobile and health in slices that reuse existing scheduler, notification, dashboard, export, and service-layer patterns.

The personal coach should be the eventual interface over trustworthy structured data, not the reason to skip the product fundamentals.
