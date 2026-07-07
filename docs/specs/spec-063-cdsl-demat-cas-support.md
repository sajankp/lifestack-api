# Spec-063: CDSL Demat CAS Support (Holdings Verification)

**Created:** 2026-07-07
**Status:** Proposed — pending owner approval before code (change-control gate)
**Depends on:** spec-060 (NSDL Demat CAS holdings verification — pipeline, table, and UI this spec extends)

---

## Problem

Spec-060 shipped Demat CAS holdings verification for **NSDL** statements only, deliberately
deferring **CDSL** as a follow-up because the two depositories' CAS PDFs differ enough to need
their own parsing fixtures (single "CDSL" section header, different column order than NSDL's
`Equities (E)` table — noted in spec-060's problem statement). CDSL is the other major Indian
depository; an owner or reader whose demat account sits at a CDSL-participating broker (a large
share of retail brokers) cannot use the verification feature at all today.

Spec-060 also gated CDSL behind "once NSDL is proven against a real statement" — as of 2026-07-07,
NSDL is still only golden-tested against synthetic `reportlab` fixtures, not a real depository PDF.
**Owner decision (2026-07-07): don't wait for that gate.** Ship CDSL now on the same best-effort
synthetic-fixture basis NSDL used, and treat the owner's first real CDSL statement as the
validation step — fix whatever the regex gets wrong against real data rather than trying to
perfect it against more synthetic fixtures first.

## Solution

Extend the existing `investing-demat-cas` module and pipeline (spec-060) to also recognize and
parse CDSL-format statements. No new `ImportModule`, no new table — this is a parser and
`source`-value extension, not a new import type.

### Registrar detection

`app/imports/demat_cas_parser.py` currently assumes NSDL's header shape. Add CDSL detection:
inspect the extracted PDF text for a distinguishing header token (NSDL statements say `NSDL Demat
Account`; CDSL statements identify themselves via a `CDSL`-labeled section/header — exact anchor
string to be confirmed against the owner's first real statement, since no real CDSL fixture exists
yet). Route to a registrar-specific line-matching regex based on which header is found; if neither
matches, fail with the existing "unexpected format" `ValidationError` rather than guessing.

### CDSL row format (best-effort, unconfirmed against a real statement)

Based on publicly documented CDSL CAS structure: a single holdings section (no `Equities (E)` /
mutual-fund split the way NSDL's combined CAS has one) with columns in a different order than
NSDL's `ISIN / Security / Current Bal. / Market Price / Value`. The parser should extract the same
three fields (`isin`, `security_name`, `quantity`) via a CDSL-specific regex, tolerant of the
column-order difference. **This regex is the part most likely to need a follow-up fix once tested
against a real CDSL PDF** — that's expected and acceptable per the owner's "ship it, iterate on
real feedback" call, not a spec gap to close before merging.

### Everything else is unchanged from spec-060

- Same `extra_json.target_account_id`, same `file_password` handling (never persisted, forwarded
  through `run_background_validate`), same wrong-password `ValidationError` path.
- Same preview comparison logic (`match` / `quantity_drift` / `missing_in_lifestack` /
  `missing_at_depository`) against `Holding.quantity` — registrar-agnostic once rows are parsed.
- Same commit behavior: one `holding_verifications` row per commit. `source` is now populated as
  `"cdsl_cas"` for CDSL-detected statements (`source` is already a plain string column per
  spec-060, not an enum, so this needs no migration).
- Same rollback (`source_import_id`-keyed delete).
- UI: no new picker — registrar is auto-detected from the PDF header text (owner decision,
  2026-07-07). Ambiguous/unrecognized header text hard-fails with the existing "unexpected format"
  `ValidationError` rather than silently mis-routing to the wrong parser.

## Golden tests

Same synthetic-`reportlab`-fixture approach as spec-060, adapted to a CDSL-shaped layout:

1. All-match CDSL fixture → every row `match`, `source="cdsl_cas"` on the persisted row.
2. Quantity drift + split hint (same corporate-action-suspected logic, registrar-agnostic).
3. Both `missing_*` directions in one CDSL statement.
4. Wrong password → same clean `ValidationError` path as NSDL.
5. Registrar misdetection (neither NSDL nor CDSL header found) → clean "unexpected format" error,
   not a silent wrong-parse.
6. Rollback deletes the verification row (shared logic with NSDL, so this may already be covered —
   confirm rather than duplicate).

**Acceptance bar for this spec is intentionally lower than spec-060's:** synthetic fixtures proving
the plumbing (detection routing, `source` value, preview/commit/rollback) is correct, *not* a
guarantee the regex matches a real CDSL PDF byte-for-byte. That gap is closed by the owner's first
real test, not by more synthetic fixtures — track any regex fix needed after that test as a normal
bug-fix commit against this spec's branch/PR, not a new spec.

## Out of scope

- **Perfecting the CDSL regex before any real statement has been tried against it** — explicitly
  rejected per the owner's 2026-07-07 call; ship best-effort, fix from real feedback.
- **Order inference / price backfill from CDSL movements** — same rejection as spec-060; a CDSL CAS
  has no price either.
- **Auto-creating `CorporateAction` or orders from CDSL drift** — same as spec-060, report-only.
- **Multi-account / multi-registrar statements in one PDF** — same as spec-060, single target
  account per commit.

## Resolved questions

1. **NSDL vs CDSL detection** — auto-detect from PDF header text (owner, 2026-07-07); hard-fail on
   ambiguity rather than silently mis-parsing. No new UI picker.
2. **Real CDSL statement availability** — owner confirmed (2026-07-07) a real CDSL CAS statement
   will be available to test soon after this ships. Budget a quick follow-up fix pass once that
   test happens; don't over-invest in perfecting the regex against synthetic fixtures alone before
   then.
