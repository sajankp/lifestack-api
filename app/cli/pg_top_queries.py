"""CLI: print the most expensive SQL statements from pg_stat_statements (spec-112).

    python -m app.cli.pg_top_queries            # top 20 by total execution time
    python -m app.cli.pg_top_queries --limit 50

On the production VM:  docker exec lifestack-api-api-1 python -m app.cli.pg_top_queries

Read-only. Statement text is normalised by Postgres (no bind values), so output holds no
amounts, names or tokens. Exit code 0 on a report, 2 when the extension is unavailable.
"""

import argparse
import asyncio

from app.core.database import postgres
from app.observability.pg_stat import fetch_statements, format_report


async def _run(limit: int) -> int:
    try:
        async with postgres.engine.connect() as conn:
            rows = await fetch_statements(conn, limit)
    finally:
        await postgres.engine.dispose()
    print(format_report(rows, limit))
    return 2 if rows is None else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--limit", type=int, default=20, help="statements to show (default 20)")
    args = parser.parse_args(argv)
    return asyncio.run(_run(max(1, args.limit)))


if __name__ == "__main__":
    raise SystemExit(main())
