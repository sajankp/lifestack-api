"""TTL-only, fail-open response cache for expensive aggregate GET endpoints (spec-087)."""

from __future__ import annotations

import json
import logging

import redis.asyncio as redis

logger = logging.getLogger(__name__)


class ResponseCache:
    """Cache-aside wrapper around Redis for read-heavy, slow-changing aggregate responses.

    No invalidation hooks by design (spec-087 "Scope"): wiring cache invalidation into every
    mutation across modules that must not import each other is a much larger, higher-risk change
    than the TTL-only staleness a personal-finance dashboard can tolerate. Any Redis error is
    caught and treated as a miss/no-op — caching must never become a hard dependency.
    """

    def __init__(self, redis_url: str, enabled: bool):
        self._redis_url = redis_url
        self.enabled = enabled
        self._client: redis.Redis | None = None

    def _get_client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.Redis.from_url(self._redis_url, decode_responses=True)
        return self._client

    async def get_json(self, key: str) -> dict | None:
        if not self.enabled:
            return None
        try:
            raw = await self._get_client().get(key)
        except Exception:
            logger.warning("response cache GET failed for key=%s, treating as miss", key)
            return None
        if raw is None:
            return None
        return json.loads(raw)

    async def set_json(self, key: str, value: dict, ttl_seconds: int) -> None:
        if not self.enabled:
            return
        try:
            await self._get_client().set(key, json.dumps(value, default=str), ex=ttl_seconds)
        except Exception:
            logger.warning("response cache SET failed for key=%s, skipping", key)
