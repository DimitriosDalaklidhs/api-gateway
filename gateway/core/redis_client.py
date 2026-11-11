"""
Redis connection pool shared across the application.
"""

import logging
import time

import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from core.config import settings

logger = logging.getLogger("gateway.redis")

# What a Redis outage looks like to callers: redis-py's own errors, plus OSError
# for socket failures that reach us unwrapped. Code that fails open catches these.
REDIS_ERRORS = (RedisError, OSError)

# Redis sits next to the gateway and every command used here is O(1), so one
# that takes a second is an outage, not a slow command. It also has to stay
# well under the probe timeout in k8s/30-gateway.yaml: /admin/health pings Redis.
_TIMEOUT_SECONDS = 1

# How long Redis is left alone after a command could not reach it
_COOLDOWN_SECONDS = 5


class FailFastRedis(aioredis.Redis):
    """
    A Redis client that stops trying for a few seconds after it can't get through.

    A Redis that is down refuses the connection at once, so callers fail open
    at once. A Redis that hangs is worse: every command waits out its timeout
    first, and one proxied request makes several. So after a command times out
    or can't connect, the commands that follow fail immediately until the
    cooldown has passed. Then the next one tries for real again.

    Pipelines don't go through execute_command and are not covered. Only the
    admin API uses one, and it reports Redis errors instead of failing open.
    """

    _down_until = 0.0  # time.monotonic() value; 0 means "not marked down"

    async def execute_command(self, *args, **options):
        if time.monotonic() < self._down_until:
            raise RedisConnectionError("Redis marked down after a failed command; not trying yet")
        try:
            return await super().execute_command(*args, **options)
        except (RedisConnectionError, RedisTimeoutError, OSError):
            self._down_until = time.monotonic() + _COOLDOWN_SECONDS
            logger.warning("Redis unreachable, skipping it", extra={"for_seconds": _COOLDOWN_SECONDS})
            raise


_pool: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Return (and lazily create) the shared Redis connection."""
    global _pool
    if _pool is None:
        _pool = FailFastRedis.from_url(
            settings.redis.url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=_TIMEOUT_SECONDS,
            socket_timeout=_TIMEOUT_SECONDS,
            # A timed-out command is not sent again: the caller fails open
            # either way, so a second wait buys nothing
            retry_on_timeout=False,
        )
        logger.info("Redis pool initialised", extra={"url": settings.redis.url})
    return _pool


async def close_redis() -> None:
    global _pool
    if _pool:
        await _pool.aclose()
        _pool = None
        logger.info("Redis pool closed")
