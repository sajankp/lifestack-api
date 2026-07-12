# Spec-075: Currency Display Consistency and Historical FX Replay

**Created:** 2026-07-12
**Status:** Draft
**Depends on:** spec-047 (multi-currency net worth), spec-072 (historical FX ingestion — the rate store this spec replays from), spec-065 (net-worth history), finance display settings (e2e-covered)
**Scope:** multi-repo, user-facing — `lifestack-web` (display profile, formatting audit) + `lifestack-api` (rate-provenance fields, as-of conversion). Two PRs, api first.

## Problem

Sequence #2 of the 2026-07-12 owner-decided backlog. Two distinct gaps hide under "currency polish":

1. **Frontend-wide formatting inconsistency.** Money/date/number formatting is mostly centralized
   (`src/utils/numberFormat.ts` `formatCurrency` via `Intl.NumberFormat(undefined, ...)`,
   `src/utils/dateFormat.ts`), but the locale is implicitly the browser's (`undefined`), a handful
   of call sites still use raw `toLocaleString`, and there is no user-facing display profile
   (locale, grouping style, decimal places) — an INR-primary user cannot choose Indian digit
   grouping (1,00,000) app-wide, and two views can disagree on grouping for the same value.
2. **Historical views convert at the wrong rate.** Views that show *historical* money converted to
   the reporting currency should use the FX rate **as of that row's date** (spec-072 made
   historical rates available), not the latest rate. Today the conversion behavior is
   inconsistent per view, and no converted figure can answer "which rate, from when, applied to
   what" — a converted historical series silently reshapes itself every time today's rate moves.

## Solution

1. **Display profile** — extend the existing finance display settings with locale/grouping and
   decimal-place preferences; `formatCurrency`/`formatNumber` read the profile instead of
   `undefined`; ESLint-guard (or review checklist) against raw `toLocaleString`/`Intl.*` outside
   the utils. Audit every page for stragglers.
2. **As-of conversion + provenance, one rate per day** — every calendar day has exactly ONE rate:
   the previous day's closing rate (owner decision 2026-07-12 — no intraday/live refresh, which
   also avoids provider rate limits). Historical rows convert at their own date's rate; "today's"
   views convert at today's rate (= yesterday's close). This collapses the historical-vs-live
   split into one rule. Every converted value carries provenance (`fx_rate`, `fx_rate_date`,
   `fx_source`) so the UI can disclose it (tooltip).
3. **Golden pin** — extend a golden scenario with an FX-rate-change day so conversion semantics
   are pinned by test, not convention.

## Backend / API / schema impact

- No schema change expected (spec-072's rate store suffices). Response schemas gain optional
  provenance fields on converted values.
- A missing historical rate must degrade explicitly (nearest-earlier rate + flagged provenance),
  never silently fall back to today's rate.

## Out of scope

- New FX sources or rate-ingestion changes (spec-072 owns ingestion).
- Reporting-currency changes mid-history semantics (workspace currency governance stays spec-022).
- Any change to *stored* amounts — this is a display/read-path spec only; ledger and snapshot rows
  are untouched (non-retroactivity discipline).

## Resolved questions (owner, 2026-07-12)

1. Locale: **explicit setting** (deterministic, testable).
2. Indian grouping: **gated on the locale setting** — active only when the explicit setting is
   Indian locale; then Indian digit grouping applies app-wide.
3. Rate model: **one rate per day = previous day's closing rate, for everything** — no live
   refresh, no historical/live split (see Solution 2).
4. Conversion cost: acceptance criterion — **at most ONE additional DB read per request**: batch
   all (currency-pair, date) needs for the response into a single query, convert from the
   in-memory map. Per-row rate queries are a rejected implementation.
