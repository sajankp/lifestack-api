import pytest
import redis.asyncio as redis

from app.core.cache import ResponseCache


def _container_url(redis_container, db: int = 3) -> str:
    return f"redis://{redis_container.get_container_host_ip()}:{redis_container.get_exposed_port(6379)}/{db}"


@pytest.mark.asyncio
async def test_get_json_miss_returns_none(redis_container):
    cache = ResponseCache(_container_url(redis_container), enabled=True)
    assert await cache.get_json("cache:v1:test:miss") is None


@pytest.mark.asyncio
async def test_set_then_get_json_roundtrip(redis_container):
    cache = ResponseCache(_container_url(redis_container), enabled=True)
    key = "cache:v1:test:roundtrip"
    value = {"total_net_worth": "123.45", "nested": {"a": 1}}

    await cache.set_json(key, value, ttl_seconds=60)
    result = await cache.get_json(key)

    assert result == value


@pytest.mark.asyncio
async def test_ttl_expiry_forces_recompute(redis_container):
    cache = ResponseCache(_container_url(redis_container), enabled=True)
    key = "cache:v1:test:ttl"
    await cache.set_json(key, {"v": 1}, ttl_seconds=60)
    assert await cache.get_json(key) is not None

    # Force immediate expiry instead of sleeping past a real TTL window.
    client = redis.Redis.from_url(_container_url(redis_container), decode_responses=True)
    await client.expire(key, -1)
    await client.aclose()

    assert await cache.get_json(key) is None


@pytest.mark.asyncio
async def test_disabled_cache_is_always_a_miss_and_never_writes(redis_container):
    url = _container_url(redis_container)
    disabled_cache = ResponseCache(url, enabled=False)
    key = "cache:v1:test:disabled"

    await disabled_cache.set_json(key, {"v": 1}, ttl_seconds=60)
    assert await disabled_cache.get_json(key) is None

    # Confirm nothing was ever written, not just that this instance won't read it.
    enabled_cache = ResponseCache(url, enabled=True)
    assert await enabled_cache.get_json(key) is None


@pytest.mark.asyncio
async def test_fail_open_on_unreachable_redis():
    # Port 1 on localhost is not a Redis server — connection is refused immediately.
    cache = ResponseCache("redis://localhost:1/0", enabled=True)

    assert await cache.get_json("cache:v1:test:unreachable") is None
    # Must not raise.
    await cache.set_json("cache:v1:test:unreachable", {"v": 1}, ttl_seconds=60)


@pytest.mark.asyncio
async def test_get_json_fails_open_on_corrupted_cached_value(redis_container):
    """Regression test: json.loads used to run outside the try/except, so a
    corrupted cache entry (never written by set_json, e.g. hand-edited or
    partially written) would raise JSONDecodeError instead of the documented
    fail-open miss."""
    url = _container_url(redis_container)
    client = redis.Redis.from_url(url, decode_responses=True)
    key = "cache:v1:test:corrupted"
    await client.set(key, "{not valid json", ex=60)
    await client.aclose()

    cache = ResponseCache(url, enabled=True)
    assert await cache.get_json(key) is None


@pytest.mark.asyncio
async def test_workspace_scoped_keys_do_not_collide(redis_container):
    cache = ResponseCache(_container_url(redis_container), enabled=True)
    key_ws1 = "cache:v1:dashboard:summary:1"
    key_ws2 = "cache:v1:dashboard:summary:2"

    await cache.set_json(key_ws1, {"workspace": 1}, ttl_seconds=60)

    assert await cache.get_json(key_ws1) == {"workspace": 1}
    assert await cache.get_json(key_ws2) is None
