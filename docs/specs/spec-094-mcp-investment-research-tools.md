# Spec-094: MCP Investment Research Tools

**Status:** Implemented — pending deployment validation
**Scope:** authenticated MCP only; no voice-agent declarations or dispatcher changes
**Depends on:** spec-083 investment identifiers and constituent snapshots, spec-093 MCP workspace context

## Problem

Connected research agents need a portable way to inspect a user's holdings,
retrieve fund or ETF constituent snapshots, write sourced snapshots back to
Lifestack, and record dividend income. The REST investing module already owns
the relevant services, but these operations are not exposed through MCP.

## Goals

- Expose bounded, workspace-scoped investment holdings as a read-only MCP tool.
- Expose constituent snapshot lookup as a read-only MCP tool.
- Allow a separately authorized research agent to replace or delete a complete
  constituent snapshot with source and freshness provenance.
- Expose dividend listing and explicit-confirmation dividend creation through MCP.
- Reuse existing investing services, validation, audit behavior, and cash-balance
  side effects; do not create a second business-logic path.
- Keep all new operations out of the voice-agent declarations and dispatcher.

## Tools

### Read-only (`mcp:read`)

- `list_investment_holdings`: bounded holdings with stable public identifiers,
  instrument metadata, account, valuation, and source metadata.
- `get_investment_constituents`: latest snapshot on or before a requested date,
  optionally filtered by source.
- `list_investment_dividends`: bounded dividend and income-event history, with
  optional symbol and brokerage-account filters.

### Research write (`mcp:research`)

- `write_investment_constituent_snapshot`: replace one instrument/date/source
  snapshot. Every row must have a ticker or ISIN; weights must sum to roughly
  100% unless the existing explicit renormalisation option is requested.
- `delete_investment_constituent_snapshot`: delete one complete snapshot only
  after explicit confirmation.

### Financial write (`mcp:write`)

- `create_investment_dividend`: create one dividend, interest, or coupon event
  only after explicit confirmation. Reuse the existing service so brokerage
  account validation and the linked investing-cash credit remain authoritative.

## Safety

- All tools require authenticated workspace membership.
- Research snapshot writes use a dedicated `mcp:research` scope, separate from
  ordinary financial writes.
- Snapshot writes preserve `source`, `as_of_date`, and `fetched_at`; replacing
  a snapshot is idempotent for the same instrument/date/source.
- Research tools never modify holdings, orders, prices, or cash balances.
- Dividend creation requires confirmation and may update the brokerage cash
  balance through the existing service.
- No raw credentials, account numbers, or provider tokens are returned.

## Acceptance criteria

- MCP registry exposes all six new tools.
- Voice tool declarations and dispatch remain unchanged.
- Holdings include identifiers sufficient for a research agent to resolve an
  ETF or mutual fund reliably.
- Invalid identifiers, unsupported instrument types, invalid weights, stale or
  malformed dates, unauthorized workspaces, and missing confirmation fail safely.
- Snapshot and dividend writes preserve existing audit and financial side effects.
- Focused MCP and service tests cover scope boundaries, workspace isolation,
  replacement/deletion confirmation, and response shapes.
