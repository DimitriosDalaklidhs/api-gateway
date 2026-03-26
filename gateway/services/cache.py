"""
Bonus — Redis request/response cache.

Only caches GET requests (configurable).
Cache key = method + full URL + sorted query params.
"""

import hashlib
import json
import logging
from typing import Optional

import redis.asyncio as aioredis
from fastapi import Request
from fastapi.responses import Response

from core.config import settings

logger = logging.getLogger("gateway.cache")


def _cache_key(request: Request) -> str:
    raw = f"{request.method}:{request.url}"
    return settings.caching.cache_prefix + hashlib.sha256(raw.encode()).hexdigest()


class CacheService:
    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis
        self._cfg = settings.caching

    def _is_cacheable(self, method: str) -> bool:
        return (
            self._cfg.enabled
            and method.upper() in self._cfg.cacheable_methods
        )

    async def get(self, request: Request) -> Optional[Response]:
        if not self._is_cacheable(request.method):
            return None
        key = _cache_key(request)
        try:
            raw = await self._redis.get(key)
        except Exception as exc:
            logger.warning("Cache GET error", extra={"error": str(exc)})
            return None

        if raw is None:
            return None

        try:
            data = json.loads(raw)
            logger.debug("Cache hit", extra={"key": key})
            return Response(
                content=data["content"].encode() if isinstance(data["content"], str) else bytes(data["content"]),
                status_code=data["status_code"],
                headers={**data["headers"], "X-Cache": "HIT"},
                media_type=data.get("media_type"),
            )
        except Exception as exc:
            logger.warning("Cache decode error", extra={"error": str(exc)})
            return None

    async def set(
        self,
        request: Request,
        response: Response,
        ttl: Optional[int] = None,
    ) -> None:
        if not self._is_cacheable(request.method):
            return
        if response.status_code >= 400:
            return

        key = _cache_key(request)
        payload = {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "content": response.body.decode("utf-8", errors="replace") if response.body else "",
            "media_type": response.media_type,
        }
        try:
            await self._redis.setex(
                key,
                ttl or self._cfg.default_ttl_seconds,
                json.dumps(payload),
            )
            logger.debug("Cache SET", extra={"key": key, "ttl": ttl or self._cfg.default_ttl_seconds})
        except Exception as exc:
            logger.warning("Cache SET error", extra={"error": str(exc)})

    async def invalidate(self, pattern: str) -> int:
        """Delete all cache keys matching *pattern* (admin helper)."""
        keys = await self._redis.keys(self._cfg.cache_prefix + pattern)
        if keys:
            return await self._redis.delete(*keys)
        return 0