"""
Redis connection pool shared across the application.
"""

import logging

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from core.config import settings

logger = logging.getLogger("gateway.redis")

# What a Redis outage looks like to callers: redis-py's own errors, plus OSError
# for socket failures that reach us unwrapped. Code that fails open catches these.
REDIS_ERRORS = (RedisError, OSError)

_pool: aioredis.Redis | None = None


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
