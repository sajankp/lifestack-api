# Spec-048: Unified Account-Centric Cash View

**Created:** 2026-07-01
**Status:** Approved (implementation)
**Depends on:** spec-008 (Investing MVP), spec-011 (transfers/FX), spec-047 (net-worth cash breakdown), reconciliation endpoint (`GET /finance/accounts/{public_id}/reconciliation`)

---

## Problem

The Investing page exposes **Orders** and **Cash Balances** as two sibling tabs that read as unrelated lists, and **Transfers** — the other primary way cash moves in/out of an account — lives on a completely different page (Spending). This produces three concrete disconnects:

1. **Orders and Cash have no visible link.** A buy order reduces brokerage cash, but the Cash tab is just a static list of snapshots with nothing tying a snapshot to the orders/transfers that moved it. The two tabs "on their own" don't answer *why is my cash what it is?*
2. **The other big cash mover is on another page.** Transfers are in Spending, so there is no single place to see everything that changed an account's cash.
3. **Reconciliation is siloed.** `GET /finance/accounts/{public_id}/reconciliation` (projected-from-flows vs latest snapshot, with a discrepancy) already exists and is surfaced only inside Spending, so brokerage cash never gets reconciled where the user manages it.

Note: the backend is already more connected than the UI implies — `CashBalance.trigger_type` records whether a snapshot was produced by a `transfer`, an `order`, or a manual entry, and per-account reconciliation is a first-class endpoint. The gap is purely in how the frontend presents this.

## Goals / non-goals

- **Goal:** Fewer top-level tabs, and a single *connected*, account-scoped surface where a snapshot, its reconciliation, and the movements (orders + transfers) that explain it live together.
- **Goal:** Preserve all existing Orders and Cash-Balance management (create/edit/delete, sorting, filters, pagination).
- **Non-goal (this pass):** Full transfer CRUD inside Investing (transfers remain managed in Spending; surfaced read-only here with a deep link). Changing the reconciliation *formula* to include order cash impact — tracked as a follow-up; today reconciliation already reflects snapshots that orders/transfers triggered via `trigger_type`.

## Solution — phased

### Phase 1 (this spec/implementation) — frontend only

Collapse Investing's top-level tabs from **Holdings · Orders · Cash Balances · Look-through Analytics** → **Holdings · Cash · Look-through Analytics**. The new **Cash** tab is a single account-scoped page:

1. **Account filter** (top): one `DropdownSelect` (reusing `accountDropdownOptions`, value = account `public_id`) that scopes the whole tab. "All accounts" by default.
2. **Reconciliation panel** (new, connective): when a specific account is selected, call `financeService.getAccountReconciliation(publicId)` and show projected balance, latest snapshot + as-of, and the discrepancy with the shared amber (minor) / rose (≥5%) color coding. This is the tissue linking snapshot ↔ flows.
3. **Cash Balances** (existing table, filtered by the account filter) with its **Add Cash Balance** flow and `trigger_type` badges intact.
4. **Orders** (existing table + full CRUD: Place Order / edit / delete, sorting, symbol/type filters, pagination) rendered under the same tab, scoped by the account filter.
5. **Transfers** (new, read-only): `financeService.getTransfers`, filtered to the selected account (`from_account_public_id` / `to_account_public_id`), shown with direction (in/out), amounts and date, plus a **Manage in Spending** deep link (`/spending` → transfers). No transfer CRUD duplicated here.

Everything is scoped by the single account filter so the sections read as one connected story rather than four independent lists.

### Phase 2 (follow-up, not implemented here)

- Merge orders + transfers into a single chronological **activity feed** per account.
- Extend the reconciliation projected-balance computation to explicitly account for order cash impact, and consider moving/ sharing the reconciliation summary so brokerage and spending reconcile through one path.

## Frontend impact (`lifestack-web`)

- `InvestingPage.tsx`:
  - Tab union `'holdings' | 'orders' | 'cash' | 'analytics'` → `'holdings' | 'cash' | 'analytics'`; `ordersRes` `enabled` switches from `tab === 'orders'` to `tab === 'cash'`.
  - `TabsList`: replace the `orders` + `cash` triggers with one `cash` trigger labelled "Cash" (keep `data-testid="investing-tab-cash"`; drop `investing-tab-orders`).
  - Merge the `orders` and `cash` `TabsContent` blocks into one `value="cash"` block; add the account filter, reconciliation panel, and transfers section.
  - New queries: per-account reconciliation (keyed by selected account, enabled only when one is selected) and transfers (enabled on the cash tab).
- Tests: update the two cases that navigate via `investing-tab-orders` to use `investing-tab-cash`; add coverage for the reconciliation panel and transfers section.

## API / schema impact

None. Reuses `GET /finance/accounts/{public_id}/reconciliation`, `GET /finance/transfers`, `GET /investing/orders`, `GET /investing/cash-balances`. No migration.

## Out of scope

- Transfer create/edit/delete inside Investing.
- Reconciliation formula changes (Phase 2).
- Backend changes of any kind.
