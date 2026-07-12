# Spec-078: Wallet Ledger Reconciliation (Statement Matching)

**Created:** 2026-07-12
**Status:** Draft
**Depends on:** spending ledger (`GET /spending/accounts/{id}/ledger`), derived wallet balance (`GET /finance/accounts/{id}/balance`), spec-074 (shared imports framework — the statement-upload vehicle), `docs/domain/cash-model-ledger-snapshots-reconciliation.md` (READ FIRST — this spec is cash-adjacent)
**Scope:** multi-repo, user-facing — `lifestack-api` (statement import module, match model, reconciliation view) + `lifestack-web` (reconciliation UI, transfer timeline).

## Problem

Sequence #5 — the highest-risk item in this wave (same domain as hardening specs 040–050).

Brokerage accounts have real reconciliation (ledger-projected vs snapshot). **Wallet/bank accounts
have nothing to reconcile against**: their balance is purely ledger-derived, so a missed or
duplicated transaction is invisible — the books are internally consistent and externally wrong.
The user's actual bank statement is the missing authoritative external source (the same
proof-methodology that made Demat CAS verification work for holdings: compare against an external
record, read-only).

## Invariants (the load-bearing section)

- **INV-1 — Matching is metadata, never mutation.** Statement matching writes ONLY new
  `statement_lines` / match-link rows. It never creates, edits, or deletes
  `spending_transactions`, `capital_transfers`, or any snapshot row. Corrections discovered by
  reconciliation flow through the normal transaction create/edit endpoints, by the user.
- **INV-2 — No snapshot semantics for wallet accounts.** `investing_cash_balances` stays
  brokerage-only. The statement's closing balance is stored on the statement record as a
  *reference value*, not as a snapshot row — introducing wallet snapshots would re-open the
  spec-050 double-count class.
- **INV-3 — Non-retroactive, like everything in this domain.** Historical ledger rows are never
  backfilled or altered by this feature. An unmatched historical gap is *displayed*, not "fixed".
- **INV-4 — Idempotent re-import.** Re-uploading an overlapping statement must not duplicate
  lines (external identity: account + date + amount + statement line ref, following the
  spec-073 `external_ref` idempotency precedent).

## Solution

1. **Statement import module** in the spec-074 shared imports framework (template → validate →
   preview → commit): generic CSV mapping (date, description, debit/credit, balance) in v1.
2. **Match engine (deterministic, suggest-only):** exact amount + date-window (default ±3 days)
   proposes matches; user confirms/rejects in a review UI; confirmed matches persist as links.
   One statement line ↔ one ledger event. Transfers match on either leg by querying
   `CapitalTransfer` where `from_account_id` or `to_account_id` equals the statement's account
   (note: `CapitalTransfer` has no `trigger_ref` field — that mechanism belongs to snapshot rows,
   which wallet accounts don't have; matching here is direct against the transfer record, with
   the leg recorded on the match link so a from-leg match is distinguishable from a to-leg match).
3. **Reconciliation view:** per wallet account — statement closing balance vs ledger-derived
   balance as of statement end date, unmatched statement lines (likely missing transactions),
   unmatched ledger rows in the period (possibly duplicates/errors), and a "reconciled through
   <date>" marker set when a period fully matches.
4. **Transfer timeline UX (web):** the transfer detail view gains a both-legs timeline (created →
   from-account effect → to-account effect → matched statement lines on each side).

## Backend / API / schema impact

- New tables: `account_statements` (workspace-scoped, account FK, period, closing balance,
  import batch ref) and `statement_lines` (statement FK, parsed fields, nullable
  `matched_transaction_id` / `matched_transfer_id` + exactly-one-target CHECK, unique external
  identity). Migrations with working downgrades.
- New read endpoint for the reconciliation view; match confirm/reject endpoints.
- **No changes to any existing cash write path.** The reconciliation identity of
  brokerage accounts (domain doc §3) is untouched.

## Out of scope

- Auto-creating transactions from unmatched statement lines (tempting; it collapses INV-1 —
  if wanted later, it must be its own spec routed through the normal imports commit flow).
- Bank-specific PDF/statement parsers (generic CSV v1; per-bank formats are follow-up import
  modules).
- Wallet-account snapshots or any change to net-worth composition.
- ML/fuzzy description matching (deterministic rules only; see cash-model proof discipline).

## Open questions (owner input needed)

1. Match window default ±3 days — right for your banks' value-dating? Configurable per account?
2. Should confirmed matches lock the transaction against edits that would break the match
   (recommend: no lock, but breaking edit clears the match link + flags the period unreconciled)?
3. Is the "reconciled through" marker purely informational, or should the UI warn when editing
   transactions before that date? (Recommend informational v1.)
4. Which of YOUR accounts' statement CSVs should be the golden fixtures? Two real formats are
   needed before implementation to keep the column mapping honest (redact and commit as fixtures,
   per the spec-044/046 broker-data precedent).
