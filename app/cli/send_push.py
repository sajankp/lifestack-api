"""CLI: send a Web Push message to enabled devices (spec-052).

A single-operator tool that doubles as (a) a smoke test — "is push actually
working end-to-end?" — and (b) a lightweight campaign sender. It bypasses the
Notification/NotificationDelivery queue and talks straight to the push service
via the same ``send_web_push`` used by ``push_delivery_job``, so a green run
here proves the whole VAPID + subscription + push-service path.

Runs inside the app package (``python -m app.cli.send_push``) so it has the
full app context — settings, DB engine, models — exactly like
``app.cli.run``. VAPID_* and DATABASE_URL must be set in the environment.

    # List every registered device (no send, VAPID not required)
    python -m app.cli.send_push --list

    # Dry run — resolve targets, show who would get it, send nothing
    python -m app.cli.send_push --title "Hi" --body "test" --dry-run

    # Send to all ACTIVE devices
    python -m app.cli.send_push --title "Lifestack" --body "Push works 🎉" --yes

    # Target one user / one device-label substring
    python -m app.cli.send_push --title "Hi" --user-id 1 --yes
    python -m app.cli.send_push --title "Hi" --label pixel --yes

    # Also drop an in-app notification into each recipient's centre (campaign)
    python -m app.cli.send_push --title "New feature" --body "..." --persist-in-app --yes

Exit codes: 0 = every targeted device accepted (or nothing to do / dry run /
list), 1 = at least one device failed, 2 = misconfiguration (VAPID unset).
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import select

# Registers the full table-metadata graph (users, workspaces, …) so ORM flush
# can resolve push_subscriptions' foreign keys — importing only the
# notifications models leaves those target tables unregistered and commit fails
# with NoReferencedTableError.
import app.core.database.models  # noqa: F401
from app.config import settings
from app.core.database import postgres
from app.notifications.models import PushSubscription
from app.notifications.push import send_web_push
from app.notifications.repository import NotificationRepository


def vapid_configured() -> bool:
    return bool(settings.VAPID_PRIVATE_KEY and settings.VAPID_PUBLIC_KEY and settings.VAPID_SUBJECT)


def _short(endpoint: str, n: int = 48) -> str:
    """``endpoint`` is a capability URL — never print it in full."""
    return endpoint[:n] + ("…" if len(endpoint) > n else "")


async def resolve_targets(
    session,
    *,
    user_id: int | None = None,
    workspace_id: int | None = None,
    label: str | None = None,
    include_inactive: bool = False,
) -> list[PushSubscription]:
    stmt = select(PushSubscription)
    if not include_inactive:
        stmt = stmt.where(PushSubscription.is_active.is_(True))
    if user_id is not None:
        stmt = stmt.where(PushSubscription.user_id == user_id)
    if workspace_id is not None:
        stmt = stmt.where(PushSubscription.workspace_id == workspace_id)
    stmt = stmt.order_by(
        PushSubscription.workspace_id, PushSubscription.user_id, PushSubscription.id
    )
    subs = list((await session.execute(stmt)).scalars().all())
    if label:
        needle = label.lower()
        subs = [s for s in subs if s.device_label and needle in s.device_label.lower()]
    return subs


async def send_to_devices(
    session,
    subscriptions: list[PushSubscription],
    *,
    title: str,
    body: str,
    entity_type: str | None = None,
    persist_in_app: bool = False,
    deactivate_gone: bool = True,
    on_result=None,
) -> dict:
    """Send ``payload`` to each subscription, folding per-device outcomes into a
    summary. Mutates subscription rows (last_success_at / last_failure_at /
    is_active) and, when ``persist_in_app``, creates one in-app Notification per
    unique recipient. Flushes but does NOT commit — the caller owns the txn.

    ``on_result`` is an optional ``(subscription, PushResult, deactivated)``
    callback for progress output.
    """
    payload = {
        "title": title,
        "body": body,
        "entity_type": entity_type,
        "entity_public_id": None,
    }

    sent = 0
    failed = 0
    deactivated = 0
    for s in subscriptions:
        result = await asyncio.to_thread(send_web_push, s.endpoint, s.p256dh, s.auth, payload)
        did_deactivate = False
        if result.success:
            s.last_success_at = datetime.now(UTC)
            sent += 1
        else:
            s.last_failure_at = datetime.now(UTC)
            failed += 1
            if result.gone and deactivate_gone:
                s.is_active = False
                deactivated += 1
                did_deactivate = True
        if on_result is not None:
            on_result(s, result, did_deactivate)

    persisted = 0
    if persist_in_app:
        repo = NotificationRepository(session)
        for workspace_id, user_id in sorted({(s.workspace_id, s.user_id) for s in subscriptions}):
            await repo.create_notification({
                "workspace_id": workspace_id,
                "user_id": user_id,
                "category": "campaign",
                "severity": "info",
                "title": title,
                "body": body or None,
                "module": "system",
                "entity_type": None,
                "entity_public_id": None,
            })
            persisted += 1

    await session.flush()
    return {"sent": sent, "failed": failed, "deactivated": deactivated, "persisted": persisted}


def _print_devices(subs: list[PushSubscription]) -> None:
    if not subs:
        print("  (no matching devices)")
        return
    header = f"  {'id':>4}  {'ws':>3}  {'user':>4}  {'act':>3}  {'label':<20}  endpoint"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for s in subs:
        print(
            f"  {s.id:>4}  {s.workspace_id:>3}  {s.user_id:>4}  "
            f"{'Y' if s.is_active else 'n':>3}  "
            f"{(s.device_label or '—')[:20]:<20}  {_short(s.endpoint)}"
        )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m app.cli.send_push",
        description="Send a Web Push message to enabled devices (spec-052).",
    )
    p.add_argument("--title", help="Notification title (required unless --list).")
    p.add_argument("--body", default="", help="Notification body text.")
    p.add_argument(
        "--entity-type",
        help="Optional routing hint; the service worker opens /todo for 'todo', "
        "else /notifications.",
    )

    tgt = p.add_argument_group("targeting (combine freely; all are AND filters)")
    tgt.add_argument("--user-id", type=int, help="Only this user's devices.")
    tgt.add_argument("--workspace-id", type=int, help="Only this workspace's devices.")
    tgt.add_argument(
        "--label", help="Only devices whose device_label contains this (case-insensitive)."
    )
    tgt.add_argument(
        "--include-inactive",
        action="store_true",
        help="Also target devices previously deactivated by a 404/410. Off by default.",
    )

    p.add_argument("--list", action="store_true", help="List matching devices and exit.")
    p.add_argument(
        "--dry-run", action="store_true", help="Resolve targets and print them, but send nothing."
    )
    p.add_argument(
        "--persist-in-app",
        action="store_true",
        help="Also create an in-app Notification per recipient (category='campaign', "
        "severity='info').",
    )
    p.add_argument(
        "--keep-gone",
        action="store_true",
        help="Do NOT deactivate a subscription on a 404/410 (gone) response. By "
        "default such dead endpoints are deactivated.",
    )
    p.add_argument(
        "-y", "--yes", action="store_true", help="Skip the confirmation prompt before sending."
    )
    return p.parse_args(argv)


async def main() -> None:
    args = _parse_args(sys.argv[1:])
    postgres.engine.echo = False  # quiet SQL echo (local dev sets echo=True)

    if not args.list and not vapid_configured():
        print(
            "ERROR: VAPID keys are not configured (VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY / "
            "VAPID_SUBJECT). Push is disabled in this environment — nothing would be delivered.\n"
            "See docs/PRODUCTION_DEPLOYMENT.md → 'Web push'.",
            file=sys.stderr,
        )
        sys.exit(2)

    if not args.list and not args.title:
        print("ERROR: --title is required (unless --list).", file=sys.stderr)
        sys.exit(2)

    async with postgres.async_session_maker() as session:
        subs = await resolve_targets(
            session,
            user_id=args.user_id,
            workspace_id=args.workspace_id,
            label=args.label,
            include_inactive=args.include_inactive,
        )

        if args.list:
            print(f"Registered devices ({len(subs)} match):")
            _print_devices(subs)
            return

        print(f"Targets ({len(subs)} device(s)):")
        _print_devices(subs)
        if not subs:
            return

        if args.dry_run:
            print("\n[dry-run] Nothing sent.")
            return

        if not args.yes:
            resp = input(f"\nSend to {len(subs)} device(s)? [y/N] ").strip().lower()
            if resp not in ("y", "yes"):
                print("Aborted.")
                return

        print()

        def _report(s: PushSubscription, result, deactivated: bool) -> None:
            if result.success:
                print(f"  ✓ sent    id={s.id} ({s.device_label or '—'})")
            else:
                tag = " [deactivated: gone]" if deactivated else ""
                print(
                    f"  ✗ FAILED  id={s.id} ({s.device_label or '—'}){tag}: {result.error_detail}"
                )

        summary = await send_to_devices(
            session,
            subs,
            title=args.title,
            body=args.body,
            entity_type=args.entity_type,
            persist_in_app=args.persist_in_app,
            deactivate_gone=not args.keep_gone,
            on_result=_report,
        )
        await session.commit()

        print(
            f"\nDone: {summary['sent']} sent, {summary['failed']} failed"
            + (f", {summary['deactivated']} deactivated" if summary["deactivated"] else "")
            + (
                f", {summary['persisted']} in-app notification(s) created"
                if summary["persisted"]
                else ""
            )
        )
        if summary["failed"]:
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
