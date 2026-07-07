# Spec-063: CDSL Demat CAS Support (Holdings Verification)

**Created:** 2026-07-07
**Status:** Implemented (2026-07-07, on `chore/imports-service-split` alongside the D4 `app/imports/service.py` split — bundled per owner request)
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

Same synthetic-`reportlab`-fixture approach as spec-060, adapted to a CDSL-shaped layout, added to
`app/tests/integration/test_demat_cas_import.py`:

1. `test_demat_cas_cdsl_detected_and_all_match` — all-match CDSL fixture → every row `match`,
   `source="cdsl_cas"` on the persisted `HoldingVerification`.
2. `test_demat_cas_cdsl_quantity_drift_and_split_hint` — quantity drift + split hint (same
   corporate-action-suspected logic, registrar-agnostic).
3. `test_demat_cas_cdsl_missing_both_directions` — both `missing_*` directions in one CDSL
   statement.
4. `test_demat_cas_unrecognized_registrar_rejected_cleanly` — a statement naming neither NSDL nor
   CDSL → clean `ValidationError` ("...could not identify it as an NSDL or CDSL statement..."),
   never a silent wrong-parse.

**Wrong-password and rollback were deliberately not duplicated for CDSL** — both are
registrar-agnostic (password failure happens during PDF extraction, before registrar detection
runs; rollback is keyed by `source_import_id`, not by `source`) and are already exercised by the
existing NSDL tests against the same code paths. Confirmed by reading the code rather than assumed.

**Acceptance bar for this spec is intentionally lower than spec-060's:** synthetic fixtures proving
the plumbing (detection routing, `source` value, preview/commit/rollback) is correct, *not* a
guarantee the regex matches a real CDSL PDF byte-for-byte. That gap is closed by the owner's first
real test, not by more synthetic fixtures — track any regex fix needed after that test as a normal
bug-fix commit against this spec's branch/PR, not a new spec.

## Implementation notes (what was actually built, 2026-07-07)

- `app/imports/demat_cas_parser.py`: the NSDL-only line-walker was refactored into a shared
  `_walk_lines(lines, section_re, holding_re)` used by both registrars, plus
  `detect_registrar(lines)` (raises `UnrecognizedRegistrarError` — a `ValueError` subclass — when
  neither or both of the literal words "NSDL"/"CDSL" appear in the extracted PDF text) and a new
  `parse_demat_cas(file_path, password)` entry point returning `(DematCasParseResult, source)`.
  `parse_demat_cas_nsdl` is kept as a thin wrapper for anyone calling it directly; a symmetrical
  `parse_demat_cas_cdsl` was added too.
- **CDSL assumptions baked into the regexes** (the part most likely to need a follow-up fix against
  a real statement): holdings section opens on a bare `CDSL` line (vs. NSDL's `Equities (E)`);
  account header accepts `BO ID:` as well as `DP ID:` but still expects a `Client ID:` on the same
  line (real CDSL statements may only carry a BO ID — unconfirmed); holding rows tolerate an
  optional leading serial number and 2-or-3-decimal quantities (NSDL is always 3). All three are
  isolated in `demat_cas_parser.py`'s regex constants, so a fix from real-statement feedback should
  be a small, contained diff.
- `app/imports/demat_cas_import.py`: `validate_demat_cas_batch` now calls `parse_demat_cas` (not
  the NSDL-only function) and catches `UnrecognizedRegistrarError` separately from the
  password/corruption branch so its error message doesn't blame the password. The detected
  `source` is threaded through `batch.extra_json["source"]` so `finalize_demat_cas_commit` can read
  it back at commit time instead of the previously hardcoded `"nsdl_cas"` literal (defaults to
  `"nsdl_cas"` if absent, for any pre-existing batch rows without the key).
- No database migration, no new `ImportModule`, no frontend changes — matches the spec's scope.

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
