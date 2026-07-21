import pytest

from app.core.retry import retry_async


class _TransientError(Exception):
    pass


class _DeterministicError(Exception):
    pass


@pytest.mark.asyncio
async def test_retry_async_succeeds_after_transient_failures():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _TransientError("boom")
        return "ok"

    result = await retry_async(
        flaky, attempts=3, base_delay_seconds=0, transient_exceptions=(_TransientError,)
    )

    assert result == "ok"
    assert calls == 3


@pytest.mark.asyncio
async def test_retry_async_raises_after_exhausting_attempts():
    calls = 0

    async def always_flaky():
        nonlocal calls
        calls += 1
        raise _TransientError("boom")

    with pytest.raises(_TransientError):
        await retry_async(
            always_flaky, attempts=3, base_delay_seconds=0, transient_exceptions=(_TransientError,)
        )

    assert calls == 3


@pytest.mark.asyncio
async def test_retry_async_reraises_deterministic_exception_immediately():
    calls = 0

    async def deterministic_failure():
        nonlocal calls
        calls += 1
        raise _DeterministicError("nope")

    with pytest.raises(_DeterministicError):
        await retry_async(
            deterministic_failure,
            attempts=3,
            base_delay_seconds=0,
            transient_exceptions=(_TransientError,),
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_retry_async_backoff_delays_double_each_attempt(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("app.core.retry.asyncio.sleep", fake_sleep)

    calls = 0

    async def always_flaky():
        nonlocal calls
        calls += 1
        raise _TransientError("boom")

    with pytest.raises(_TransientError):
        await retry_async(
            always_flaky,
            attempts=3,
            base_delay_seconds=2.0,
            transient_exceptions=(_TransientError,),
        )

    assert sleeps == [2.0, 4.0]


@pytest.mark.asyncio
async def test_retry_async_invokes_on_retry_callback():
    seen: list[int] = []

    async def flaky():
        if len(seen) < 2:
            raise _TransientError("boom")
        return "ok"

    def on_retry(attempt: int, exc: Exception) -> None:
        seen.append(attempt)

    result = await retry_async(
        flaky,
        attempts=3,
        base_delay_seconds=0,
        transient_exceptions=(_TransientError,),
        on_retry=on_retry,
    )

    assert result == "ok"
    assert seen == [1, 2]
