import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

OnRetryCallback = Callable[[int, Exception], None]


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential-backoff policy for opted-in, idempotent jobs
    (spec-088). Only exceptions in ``transient_exceptions`` are retried."""

    attempts: int
    base_delay_seconds: float
    transient_exceptions: tuple[type[Exception], ...]


# Exceptions that mean "transient, worth another try" for the opted-in
# external-API jobs (spec-088): fx_rate_ingestion_job, bhavcopy_price_feed_job,
# investment_closing_prices_job. httpx.HTTPError covers both transport-level
# failures (timeouts, connection resets) and non-2xx HTTPStatusError; a bare
# TimeoutError/ConnectionError covers non-httpx blocking calls.
TRANSIENT_JOB_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.HTTPError,
    TimeoutError,
    ConnectionError,
)


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay_seconds: float,
    transient_exceptions: tuple[type[Exception], ...],
    on_retry: OnRetryCallback | None = None,
) -> T:
    """Retry ``fn`` only on ``transient_exceptions`` with exponential backoff
    (``base_delay_seconds * 2**(n-1)``). Any other exception re-raises immediately —
    a deterministic failure (bad data, constraint violation) can't be fixed by retrying.
    Re-raises the last transient exception once ``attempts`` are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except transient_exceptions as exc:
            last_exc = exc
            if attempt == attempts:
                raise
            if on_retry is not None:
                on_retry(attempt, exc)
            await asyncio.sleep(base_delay_seconds * (2 ** (attempt - 1)))
    # Unreachable: the loop always either returns or raises on the last attempt.
    raise last_exc  # type: ignore[misc]
