"""Read-only: report brokerage accounts whose latest cash snapshot disagrees with
the transfers, orders and dividends that should produce it.

    uv run python -m scripts.audit_brokerage_cash [--workspace-id N] [--all]

Dry-run by default: never writes. For each drifting account it prints the
correcting snapshot that *would* bring the balance in line.

To apply one correction after reviewing it (one account/currency per run):

    ... --apply --workspace-id N --account-id A --currency USD --expected 10.72

``--expected`` must equal the value the dry-run printed; if the data has moved
since, the script refuses. It appends a single manual snapshot (history is never
edited), so it is reversible by appending another. See app/investing/cash_audit.py.
"""

import argparse
import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from app.core.database import postgres
from app.investing.cash_audit import CorrectionRefusedError, apply_correction, audit_brokerage_cash


async def _run(workspace_id: int | None, show_all: bool) -> int:
    async with postgres.async_session_maker() as session:
        rows = await audit_brokerage_cash(session, workspace_id)
    shown = [r for r in rows if show_all or r.drift != 0]
    print(
        f"{len(rows)} brokerage account/currency series checked, {len([r for r in rows if r.drift != 0])} drifting"
    )
    for r in shown:
        anchor = r.anchor_created_at.isoformat() if r.anchor_created_at else "none (from zero)"
        print(
            f"ws={r.workspace_id} acct_id={r.account_id} {r.account_name} [{r.currency}] "
            f"stored={r.latest_balance} expected={r.expected_balance} drift={r.drift:+} "
            f"anchor={anchor}"
        )
        if r.drift != 0:
            print(
                f"    proposed (NOT applied): --apply --workspace-id {r.workspace_id} --account-id {r.account_id} --currency {r.currency} --expected {r.expected_balance}  -> manual snapshot of {r.expected_balance} "
                f"{r.currency} to {r.account_name}"
            )
    return 1 if any(r.drift != 0 for r in rows) else 0


async def _apply(args: argparse.Namespace) -> int:
    ref = f"audit-repair-{datetime.now(UTC):%Y%m%d}"
    async with postgres.async_session_maker() as session:
        try:
            row = await apply_correction(
                session,
                workspace_id=args.workspace_id,
                account_id=args.account_id,
                currency=args.currency,
                reviewed_expected=Decimal(args.expected),
                source_ref=ref,
            )
        except CorrectionRefusedError as exc:
            print(f"REFUSED: {exc}")
            return 2
        await session.commit()
    print(
        f"applied: appended snapshot {row.public_id} balance={row.balance} {row.currency} ({ref})"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace-id", type=int, default=None)
    parser.add_argument("--all", action="store_true", help="also list accounts with zero drift")
    parser.add_argument("--apply", action="store_true", help="append the correcting snapshot")
    parser.add_argument("--account-id", type=int, help="internal account id (with --apply)")
    parser.add_argument("--currency", help="series currency (with --apply)")
    parser.add_argument("--expected", help="reviewed expected balance (with --apply)")
    args = parser.parse_args()
    if args.apply:
        needed = (args.workspace_id, args.account_id, args.currency, args.expected)
        if any(v is None for v in needed):
            parser.error("--apply needs --workspace-id, --account-id, --currency and --expected")
        raise SystemExit(asyncio.run(_apply(args)))
    raise SystemExit(asyncio.run(_run(args.workspace_id, args.all)))


if __name__ == "__main__":
    main()
