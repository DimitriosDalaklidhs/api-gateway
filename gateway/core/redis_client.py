"""
Redis connection pool shared across the application.
"""

import logging
from typing import Optional

import redis.asyncio as aioredis

from core.config import settings

logger = logging.getLogger("gateway.redis")

_pool: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    """Return (and lazily create) the shared Redis connection."""
    global _pool
    if _pool is None:
        _pool = aioredis.from_url(
            settings.redis.url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            retry_on_timeout=True,
        )
        logger.info("Redis pool initialised", extra={"url": settings.redis.url})
    return _pool


async def close_redis() -> None:
    global _pool
    if _pool:
        await _pool.aclose()
        _pool = None
        logger.info("Redis pool closed")