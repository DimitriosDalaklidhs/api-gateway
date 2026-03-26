"""
Phase 3 — Redis-backed rate limiter.

Algorithm: sliding-window counter per IP (or IP+path).
Also supports temporary IP bans.
"""

import logging
import time
from typing import Tuple

import redis.asyncio as aioredis

from core.config import settings

logger = logging.getLogger("gateway.rate_limiter")

_INCR_SCRIPT = """
local key   = KEYS[1]
local limit = tonumber(ARGV[1])
local win   = tonumber(ARGV[2])
local count = redis.call('INCR', key)
if count == 1 then
    redis.call('EXPIRE', key, win)
end
return count
"""


class RateLimiter:
    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis
        self._cfg = settings.rate_limiting
        self._script_sha: str | None = None

    async def _load_script(self) -> str:
        if not self._script_sha:
            self._script_sha = await self._redis.script_load(_INCR_SCRIPT)
        return self._script_sha

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def is_banned(self, ip: str) -> bool:
        """Return True when the IP is in the temporary ban list."""
        key = f"ban:{ip}"
        return bool(await self._redis.exists(key))

    async def ban(self, ip: str, duration: int | None = None) -> None:
        """Temporarily ban an IP."""
        dur = duration or self._cfg.ban_duration_seconds
        key = f"ban:{ip}"
        await self._redis.setex(key, dur, "1")
        logger.warning("IP banned", extra={"ip": ip, "duration": dur})

    async def check(
        self,
        ip: str,
        route_limit: int | None = None,
        path: str = "",
    ) -> Tuple[bool, int, int]:
        """
        Check & increment the counter for *ip*.

        Returns:
            allowed  – True if under the limit
            count    – current request count in the window
            limit    – effective limit that was applied
        """
        if not self._cfg.enabled:
            return True, 0, 0

        # Per-route limit overrides global default
        limit = route_limit if route_limit is not None else self._cfg.default_limit
        window = self._cfg.window_seconds

        key = f"rl:{ip}"

        try:
            sha = await self._load_script()
            count = int(
                await self._redis.evalsha(sha, 1, key, limit, window)  # type: ignore[arg-type]
            )
        except Exception as exc:
            # If Redis is down, fail-open (don't block all traffic)
            logger.error("Rate limiter Redis error — fail-open", extra={"error": str(exc)})
            return True, 0, limit

        allowed = count <= limit
        if not allowed:
            logger.warning(
                "Rate limit exceeded",
                extra={"ip": ip, "count": count, "limit": limit, "path": path},
            )
        return allowed, count, limit

    async def remaining(self, ip: str, route_limit: int | None = None) -> int:
        limit = route_limit if route_limit is not None else self._cfg.default_limit
        key = f"rl:{ip}"
        count_str = await self._redis.get(key)
        count = int(count_str) if count_str else 0
        return max(0, limit - count)

    async def reset(self, ip: str) -> None:
        """Remove rate-limit counter for an IP (admin / test helper)."""
        await self._redis.delete(f"rl:{ip}")

    async def get_stats(self, ip: str, route_limit: int | None = None) -> dict:
        limit = route_limit if route_limit is not None else self._cfg.default_limit
        key = f"rl:{ip}"
        pipe = self._redis.pipeline()
        pipe.get(key)
        pipe.ttl(key)
        count_str, ttl = await pipe.execute()
        count = int(count_str) if count_str else 0
        return {
            "ip": ip,
            "count": count,
            "limit": limit,
            "remaining": max(0, limit - count),
            "reset_in_seconds": ttl,
            "banned": await self.is_banned(ip),
        }