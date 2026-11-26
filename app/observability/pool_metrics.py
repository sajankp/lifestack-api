"""Connection-pool saturation gauges (spec-112), evaluated at scrape time."""

from prometheus_client import Gauge

db_pool_size = Gauge("lifestack_db_pool_size", "Configured SQLAlchemy pool size")
db_pool_checked_out = Gauge(
    "lifestack_db_pool_checked_out", "Connections currently checked out of the pool"
)
db_pool_overflow = Gauge(
    "lifestack_db_pool_overflow", "Connections open beyond pool_size (0 when none)"
)


def register_pool_metrics(pool) -> bool:
    """Point the gauges at `pool`. Returns False for pools that do not report these numbers
    (e.g. NullPool in tests), so callers never fail on them."""
    if not all(callable(getattr(pool, name, None)) for name in ("size", "checkedout", "overflow")):
        return False
    db_pool_size.set_function(lambda: float(pool.size()))
    db_pool_checked_out.set_function(lambda: float(pool.checkedout()))
    # SQLAlchemy reports -pool_size while the pool is idle; only real overflow is interesting.
    db_pool_overflow.set_function(lambda: float(max(0, pool.overflow())))
    return True
