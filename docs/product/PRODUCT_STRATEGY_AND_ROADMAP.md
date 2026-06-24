# Product Strategy and Roadmap

Date: 2026-06-03
Last updated: 2026-06-14

Scope: current product positioning, implementation status, post-Gate 0 backlog, and staged roadmap for mobile, health tracking, medication reminders, workout tracking, document intelligence, second brain, and personal coach workflows.

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
| Voice/capture | Experimental input layer over structured services. | Experimental |

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
| Archived or partial capture path | [`Spec 018`](../specs/spec-018-quick-capture.md), [`Spec 021`](../specs/spec-021-voice-agent-function-calling.md) | Spec 018 quick capture is archived/deferred to roadmap. Spec 021 voice/tool calling is implemented as Phase 1; production capture expansion remains a roadmap item. |
| Proposed/current slices | [`Spec 030`](../specs/spec-030-cli-management-commands.md) | CLI management commands remain a proposed/current implementation candidate. |
| Deferred future data model | [`Spec 035`](../specs/spec-035-platform-market-data-curation.md) | Platform market-data curation is a deferred backlog item until the workspace-scoped investing flows, permission model, and provenance requirements are ready. |

## 4) Near-Term Roadmap

This is the Post-Gate 0 roadmap backlog, promoted near the top because it contains the next practical product slices. These items should deepen the current finance-led product before Lifestack expands into new life domains.

### Immediate Focus

1. Keep investing look-through improvements focused on accuracy, correction flows, and visible data quality before adding global/shared data ownership.
2. Polish the current daily-work surfaces: todos, recurring todos, imports, investing forms, and dashboard cues.
3. Treat [`Spec 033`](../specs/spec-033-hybrid-instrument-catalog.md) hybrid instrument catalog as the next data-model decision point after workspace-scoped investing flows settle.
4. Defer platform-wide constituent and price-data curation until there is a clear admin persona, provenance model, and rollback workflow.

### Core Product Depth

| Area | Roadmap Item | Why It Belongs Here |
|---|---|---|
| Spending analytics | Category breakdown, budget-vs-actual analytics, savings-rate analytics, and richer trend UX. | These are product-surface expansions beyond the implemented trends slice. |
| Wallet ledger | Ledger-style balance projection, richer transfer timeline UX, reconciliation, and statement matching. | These are finance-product depth items, not blockers for the current demo baseline. |
| Notifications | Email delivery, push delivery, real-time notification transport, grouping, and digest variants. | Delivery channels depend on mobile/email infrastructure and should be sequenced with notification strategy. |
| Imports | Very-large-file streaming guarantees, async/background import workers, `.xlsx` imports, smart column mapping, partial-success modes, and virus scanning. | These are scale/operations upgrades beyond the implemented CSV validate-preview-commit workflow. |
| Currency display | Remaining frontend-wide display polish, locale/date/number profiles, and historical FX replay for every view. | These are consistency and polish tracks after the implemented finance settings foundation. |
| Voice/capture | WebRTC-grade production transport, broader capture domains, multi-item capture, and AI-assisted routing. | Capture is useful as an input layer, but expansion should follow mobile/coach sequencing. |
| Weekly summaries | Configurable summary cadence, regeneration/admin correction flows, and expanded insight surfaces. | These are workflow-product improvements, not changes to the implemented weekly-summary contract. |
| Budget model | Grouped budgets and custom financial KPIs. | These are product-model expansions that should be designed intentionally. |

### Investing and Market Data

| Area | Roadmap Item | Why It Belongs Here |
|---|---|---|
| Investing performance | Richer return math, deeper visualization, benchmark comparison, dividend/total-return views, and scheduled/background price-refresh cadence. | On-demand automated price refresh is implemented; deeper performance analytics and scheduled pricing should be scoped as explicit product slices. |
| Hybrid instrument catalog | [`Spec 033`](../specs/spec-033-hybrid-instrument-catalog.md): global public instruments/companies with workspace-scoped tenant overrides. | This reduces duplicate public securities and redundant provider calls, but should wait until the current workspace-scoped investing flows settle. |
| Look-through analytics | UX alerts, quality scoring, company identity normalization, derivative look-through, and deeper constituent-provider coverage. | Look-through analytics and automated ETF/MF constituent ingestion are implemented; these are advanced accuracy, scale, and UX tracks after V1 correctness. |
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

| Era | Product Name | Purpose |
|---|---|---|
| Gate 0 | Foundation | Make the current product secure, reliable, demoable, and honest in docs. |
| Track 1 | Mobile Companion | Move reminders, capture, camera upload, and health sync to the device people carry. |
| Track 2 | Health Memory | Add health metrics, medications, workouts, and source-aware longitudinal records. |
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

### Track 1: Mobile Companion Foundation

Goal: make the phone the natural capture and sync surface.

Scope:

- Mobile app shell with shared design language.
- Quick capture for todo, spending, and journal-like notes.
- Push notifications for reminders and summaries.
- Camera upload for documents.
- Background sync plumbing.
- Health-app sync architecture for Apple Health, Google Health Connect, or equivalent providers.

Non-goals:

- Full health analytics.
- General personal coach automation.
- Multi-user SaaS mobile features.

### Track 2: Health Memory

Goal: support manual health tracking before depending on external sync.

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
- Capture health metrics, sleep, weight, medication reminders, workouts, health-app sync, documents/RAG, second brain, and personal coach as planned future tracks.
- Avoid claiming health/documents/memory are implemented today.
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
