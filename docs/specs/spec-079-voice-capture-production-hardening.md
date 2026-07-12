# Spec-079: Voice/Capture Production Hardening

**Created:** 2026-07-12
**Status:** Draft
**Depends on:** spec-021 (voice agent function calling, Phase 1), spec-039 (ADK evaluation — verdict "no, not now" stands), spec-059/061/066 (capture usability + consolidation)
**Scope:** multi-repo, user-facing — `lifestack-api` (`app/capture/`) + `lifestack-web` (capture surface). Voice stays labeled **experimental** until the eval milestone below is met (positioning rule).

## Problem

Sequence #6 — largest and most open-ended; deliberately last. The capture surface works
(WS transport with frame/session caps; tools for todos, spending transactions, medication/weight
logging, investing summary) but is experimental-grade on three axes:

1. **Transport** — the raw WebSocket audio path degrades on lossy networks; no reconnect/resume,
   no adaptive audio. "WebRTC-grade" is the roadmap phrase, but WebRTC itself is a large
   dependency decision.
2. **Multi-item capture** — one utterance mapping to N actions ("add milk and eggs, and log 40
   for lunch") is not supported; each session handles single-intent flows.
3. **Routing confidence** — there is no measured tool-call accuracy, so no basis for widening the
   surface or claiming production quality (docs-and-positioning claims discipline).

## Solution (three gated stages — each stage is a merge point, later stages can be dropped)

**Stage A — Measure first (no product change).** Build the eval set spec-039 scaffolded:
utterances → expected tool calls, run against the current engine, publish accuracy in the spec.
This number gates everything: no transport or routing work is justified until we know what's
actually failing (predict-before-measure discipline). Includes instrumenting WS disconnect/
resume-failure rates in production logs (PII-redacted, counts only).

**Stage B — Transport resilience within WS.** Reconnect-with-session-resume, client-side audio
buffering across gaps, explicit session-state surfacing in the UI. WebRTC is NOT adopted in this
spec — it's a new-dependency decision (owner rule) that Stage A data must justify or kill; if the
measured failure mode is "sessions die on network blips", resume-on-WS may close it for one user
on known networks.

**Stage C — Multi-item capture.** The agent may emit an ordered list of tool calls per utterance;
each executes through the same service-layer tools (no new write paths); the confirmation UI
shows all N proposed actions and commits selected ones. Routing stays deterministic
tool-schema-based (no free-form "AI-assisted routing" beyond the model's existing function
calling — that phrase in the roadmap is explicitly narrowed here).

## Backend / API / schema impact

- No schema changes expected. `app/capture/` only (session/agent/audio), plus eval fixtures under
  tests. Session caps (`CAPTURE_MAX_*`) stay enforced; any new limit gets a config flag defaulting
  to current behavior.

## Out of scope

- **ADK migration** — spec-039's "no, not now" stands until its release cadence stabilizes.
- **WebRTC adoption** — requires its own spec with Stage A evidence + dependency discussion.
- New capture domains beyond the existing tool set (health vitals, documents — those belong to
  their product tracks).
- Removing the "experimental" label — that happens only when Stage A accuracy is published and
  reproduced (positioning rule), as its own docs change.

## Resolved questions (owner, 2026-07-12)

1. Eval set: **50 utterances** (60% from real usage transcripts, 40% adversarial).
2. Stage C multi-item capture: **confirmed wanted** — stays in scope.
3. Experimental-label bar: **≥90% exact tool+args match, measured twice a week apart** —
   confirmed.
4. Live captions: **only if free from the existing session stream** — in scope solely if the
   current Gemini Live session already emits transcription at no additional API call/token cost;
   any implementation that adds API spend is out of scope.
