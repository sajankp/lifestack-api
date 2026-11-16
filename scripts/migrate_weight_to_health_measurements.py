"""Copy body-weight rows from ``weight_entries`` into the measurement store (spec-111).

    uv run python -m scripts.migrate_weight_to_health_measurements [--workspace-id N]
    uv run python -m scripts.migrate_weight_to_health_measurements --apply

Dry-run by default: prints, per workspace, how many rows would be copied and the latest
reading, and writes nothing. ``--apply`` copies the missing rows in one transaction, then
prints source count, target count and the latest reading so equality can be checked by a
read. It is idempotent (run it again after the deploy to sweep rows written in between),
refuses (writing nothing) if a copied row no longer matches its source, and leaves
``weight_entries`` untouched as the rollback path.
"""

import argparse
import asyncio

import app.core.database.models  # noqa: F401  (register every table so FKs resolve on INSERT)
from app.core.database import postgres
from app.health.measurements.weight_migration import (
    MigrationRefusedError,
    apply_weight_migration,
    plan_weight_migration,
)


def _fmt_latest(latest) -> str:
    if latest is None:
        return "no readings"
    kg, at = latest
    return f"{kg} kg on {at.date().isoformat()}"


async def _dry_run(workspace_id: int | None) -> int:
    async with postgres.async_session_maker() as session:
        try:
            plans = await plan_weight_migration(session, workspace_id)
        except MigrationRefusedError as exc:
            print(f"REFUSED: {exc}")
            return 1
    if not plans:
        print("No weight_entries rows found; nothing to copy.")
        return 0
    print(f"{len(plans)} workspace(s) with weight data (dry run, nothing written):")
    for p in plans:
        print(
            f"  workspace {p.workspace_id}: {p.source_count} source row(s), "
            f"{p.already_copied} already copied, {p.to_copy} to copy; "
            f"latest source: {_fmt_latest(p.latest_source)}"
        )
    print("Re-run with --apply to copy.")
    return 0


async def _apply(workspace_id: int | None) -> int:
    async with postgres.async_session_maker() as session:
        try:
            results = await apply_weight_migration(session, workspace_id)
        except MigrationRefusedError as exc:
            await session.rollback()
            print(f"REFUSED: {exc}\nNothing was written.")
            return 1
        await session.commit()
    if not results:
        print("No weight_entries rows found; nothing to copy.")
        return 0
    print(f"Copied weight data for {len(results)} workspace(s):")
    for r in results:
        extra = f" (+{r.extra_target} written after the switch)" if r.extra_target else ""
        print(
            f"  workspace {r.workspace_id}: copied {r.copied} of {r.source_count} source row(s); "
            f"new store now holds {r.target_count}{extra}; latest: {_fmt_latest(r.latest)}"
        )
    print("Verify with a read of get_health_series(body_weight) before relying on it.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace-id", type=int, help="limit to one workspace")
    parser.add_argument("--apply", action="store_true", help="copy the missing rows")
    args = parser.parse_args()
    runner = _apply if args.apply else _dry_run
    raise SystemExit(asyncio.run(runner(args.workspace_id)))


if __name__ == "__main__":
    main()
