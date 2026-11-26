"""Top statements from `pg_stat_statements` as a human-readable report (spec-112).

`pg_stat_statements` stores *normalised* statement text (`$1`, `$2`, ...), never bind values, so
the report is safe to print: no amounts, names or tokens can appear in it.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

_MAX_QUERY_CHARS = 160

_SQL = text(
    """
    SELECT s.calls, s.total_exec_time, s.mean_exec_time, s.max_exec_time, s.rows, s.query
    FROM pg_stat_statements s
    JOIN pg_database d ON d.oid = s.dbid
    WHERE d.datname = current_database()
      AND s.query NOT ILIKE '%pg_stat_statements%'
    ORDER BY s.total_exec_time DESC
    LIMIT :limit
    """
)

_HOW_TO_ENABLE = (
    "pg_stat_statements is not available on this database.\n"
    "  1. start Postgres with  -c shared_preload_libraries=pg_stat_statements  (docker-compose.yml\n"
    "     does this for the `postgres` service; a restart is required)\n"
    "  2. run  CREATE EXTENSION IF NOT EXISTS pg_stat_statements;  once, as a superuser"
)


@dataclass(frozen=True)
class StatementRow:
    calls: int
    total_ms: float
    mean_ms: float
    max_ms: float
    rows: int
    query: str


async def fetch_statements(conn: AsyncConnection, limit: int = 20) -> list[StatementRow] | None:
    """Rows ranked by total execution time, or None when the extension is not usable."""
    try:
        result = await conn.execute(_SQL, {"limit": limit})
    except DBAPIError:
        return None
    return [
        StatementRow(
            calls=int(r.calls),
            total_ms=float(r.total_exec_time),
            mean_ms=float(r.mean_exec_time),
            max_ms=float(r.max_exec_time),
            rows=int(r.rows),
            query=r.query,
        )
        for r in result
    ]


def format_report(rows: list[StatementRow] | None, limit: int = 20) -> str:
    if rows is None:
        return _HOW_TO_ENABLE
    if not rows:
        return "pg_stat_statements is enabled but has recorded no statements yet."

    shown = rows[:limit]
    listed_total = sum(r.total_ms for r in shown) or 1.0
    lines = [
        f"Top {len(shown)} statements by total execution time (share is of the listed total)",
        f"{'#':>2} {'share':>6} {'total s':>9} {'calls':>8} {'mean ms':>9} {'max ms':>9} "
        f"{'rows':>9}  query",
    ]
    for rank, r in enumerate(shown, start=1):
        query = " ".join(r.query.split())
        if len(query) > _MAX_QUERY_CHARS:
            query = query[:_MAX_QUERY_CHARS] + "…"
        lines.append(
            f"{rank:>2} {r.total_ms / listed_total * 100:5.1f}% {r.total_ms / 1000:9.2f} "
            f"{r.calls:>8} {r.mean_ms:9.1f} {r.max_ms:9.1f} {r.rows:>9}  {query}"
        )
    return "\n".join(lines)
