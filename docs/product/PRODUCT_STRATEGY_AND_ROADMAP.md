# Product Strategy and Roadmap

Date: 2026-06-03

Scope: product strategy and staged roadmap for mobile, health tracking, medication reminders, workout tracking, document intelligence, second brain, and personal coach workflows.

## 1) Product Thesis

Lifestack should evolve from a personal productivity and finance system into a private personal coach powered by structured life data.

The product should not become a generic chatbot with loose files attached. The durable differentiator is a shared data model where actions, money, health, documents, and memory can be captured, reviewed, exported, and reasoned over with clear source trails.

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

- Auth, workspace scoping, todos, spending, investing, dashboard, notifications, summaries, imports, exports, recurring workflows, and quick capture.
- A scheduler and notification model that can later support medication reminders and health follow-ups.
- A service-layer architecture that gives future AI/mobile/document adapters a stable place to call into existing product capabilities.

The main risk is sequencing. Health, documents, and memory are high-trust domains. They should be planned now but implemented only after the product has stronger reliability, security, mobile ergonomics, and E2E coverage.

## 3) Product Track Decision

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

## 4) Recommended Sequence

The roadmap can use stage numbers for planning, but user-facing product eras should have memorable names:

| Era | Product Name | Purpose |
|---|---|---|
| Gate 0 | Foundation | Make the current product secure, reliable, demoable, and honest in docs. |
| Track 1 | Capture | Make logging and review fast across web and mobile. |
| Track 2 | Mobile Companion | Move reminders, capture, camera upload, and health sync to the device people carry. |
| Track 3 | Health Memory | Add health metrics, medications, workouts, and source-aware longitudinal records. |
| Track 4 | Source-Backed Second Brain | Connect documents, notes, records, and activity through cited retrieval. |
| Track 5 | Personal Coach | Turn structured personal context into planning support and review workflows. |

### Gate 0: Product Foundation Stabilization

Goal: make the existing product credible, secure, and demoable before adding new life domains.

Acceptance criteria:

- Role-based authorization is enforced where roles already exist.
- Password policy, session controls, and security findings are addressed.
- One-command full-stack E2E path works from a clean checkout.
- Demo seed/reset flow exists.
- Mobile shell/navigation is responsive enough for quick capture.
- READMEs separate current features from planned roadmap scope.

### Track 1: Mobile Companion Foundation

Goal: make the phone the natural capture and sync surface.

Scope:

- Mobile app shell with shared design language.
- Quick capture for todo, spending, and journal-like notes.
- Push notifications for reminders and summaries.
- Camera upload for documents.
- Background sync plumbing.
- Health-provider sync architecture for Apple Health, Google Health Connect, or equivalent providers.

Non-goals:

- Full health analytics.
- General personal coach automation.
- Multi-user SaaS mobile features.

### Track 2: Health MVP

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

### Track 6: Personal Coach

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

## 5) Personal Data Trust Model

Trust is a product feature, not only an implementation detail.

- **Consent:** users choose which domains the assistant, sync jobs, and retrieval layer may use.
- **Source tracking:** every imported, synced, extracted, or assistant-used record keeps visible source metadata.
- **Review before high-impact writes:** sensitive health and financial data should require human confirmation before normalized records are created or changed.
- **Deletion and export:** health, document, memory, and coach-derived data must remain portable and removable.
- **Audit trails:** assistant actions and automated workflows should leave enough history for the user to understand what happened and why.
- **Cited responses:** document-backed and health-backed answers should cite source records instead of presenting unsupported conclusions.

## 6) PO Risks

| Risk | Why It Matters | Mitigation |
|---|---|---|
| Health privacy | Health data is high-trust and sensitive. | Ship export/delete, source metadata, and permission boundaries before broad sync. |
| False confidence from RAG | Retrieval can sound correct without being grounded. | Require citations and source links for document-backed answers. |
| Mobile dependency | Health sync needs mobile, but mobile can become a large project. | Start with mobile capture/notification shell before deep sync. |
| Scope explosion | Health, documents, and coach features can each become a full product. | Sequence tracks and use non-goals aggressively. |
| Coach safety | Recommendations can be mistaken for professional advice. | Keep coach as planning support with disclaimers, audit trails, and user-confirmed actions. |

## 7) README Update Requirements

The README should:

- Keep current implemented features separate from planned roadmap tracks.
- Capture health metrics, sleep, weight, medication reminders, workouts, health-app sync, documents/RAG, second brain, and personal coach as planned future tracks.
- Avoid claiming health/documents/memory are implemented today.
- Name the stabilization gate before new high-trust modules.
- Describe AI as an interface over structured services, not the foundation.
- Explain the trust model and coach boundaries at a high level.
- Name the flagship morning-review workflow so the roadmap feels like a product journey, not a list of modules.

## 8) PO Verdict

Proceed with the product track, but do not make it the immediate feature implementation branch.

The correct next move is to document the vision, stabilize the current foundation, then introduce mobile and health in slices that reuse existing scheduler, notification, dashboard, export, and service-layer patterns. The personal coach should be the eventual interface over trustworthy structured data, not the reason to skip the product fundamentals.
