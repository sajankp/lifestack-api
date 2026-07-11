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
2. **As-of conversion + provenance** — for each API response that returns converted historical
   values, convert using the rate effective on the row's date and include rate provenance
   (`fx_rate`, `fx_rate_date`, `fx_source`) so the UI can disclose it (tooltip). Live "current
   value" views keep using the latest rate — the split must be explicit per endpoint.
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

## Open questions (owner input needed)

1. Locale source: explicit setting (recommended — deterministic, testable) vs browser-derived
   default with override?
2. Indian grouping (lakh/crore) app-wide when locale is `en-IN`: numerals only, or also unit words
   ("₹1.2L") in compact contexts (dashboard cards)?
3. Which views are "historical" (as-of rate) vs "live" (latest rate)? Proposed: net-worth history,
   transaction/order/dividend lists → as-of; current holdings value, today's net worth → latest.
   Confirm the classification before implementation.
4. Per-row as-of conversion cost on large lists — precompute at write, join at read, or convert at
   read with a rate cache? (Recommend read-time with an in-request rate map; measure first.)
