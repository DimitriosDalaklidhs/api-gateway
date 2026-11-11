"""
Phase 3 — Redis-backed rate limiter.

Algorithm: fixed-window counter per client IP and route. The first request in a
window creates the counter with a TTL of window_seconds, and the window resets
when the key expires. A client can therefore send up to twice the limit across
a window boundary (README, Known limitations).
Also supports temporary IP bans. Nothing bans automatically: an operator sets
and lifts them through the admin API.

If Redis is unreachable, the limiter fails open: limits and bans stop applying
and traffic keeps flowing.
"""

import logging

import redis.asyncio as aioredis
from core.config import settings
from core.redis_client import REDIS_ERRORS
from redis.exceptions import NoScriptError

logger = logging.getLogger("gateway.rate_limiter")

# INCR and EXPIRE must happen together. As two separate commands, a crash
# between them leaves a counter with no TTL, and that client stays limited
# forever. A Lua script runs atomically on the Redis server.
_INCR_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
return count
"""

# Counter used when a caller doesn't name a route (tests, ad-hoc checks)
DEFAULT_ROUTE = "default"

# SHA of the loaded script, shared by every RateLimiter in this process. A new
# RateLimiter is built for each request, so caching it per instance meant a
# SCRIPT LOAD round trip on every request.
_script_sha: str | None = None


class RateLimiter:
    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis
        self._cfg = settings.rate_limiting

    @staticmethod
    def _key(ip: str, route: str) -> str:
        # One counter per IP *and* route, so traffic on one route never uses
        # up another route's limit
        return f"rl:{ip}:{route}"

    def _route_keys(self, ip: str) -> list[str]:
        """Every counter this IP can have: one per configured route, plus the default."""
        return [self._key(ip, r.path) for r in settings.routes] + [self._key(ip, DEFAULT_ROUTE)]

    def _limit(self, route_limit: int | None) -> int:
        # A route's own limit wins; a route without one gets default_limit
        return route_limit if route_limit is not None else self._cfg.default_limit

    async def _incr(self, key: str) -> int:
        global _script_sha
        window = self._cfg.window_seconds
        if _script_sha is None:
            _script_sha = await self._redis.script_load(_INCR_SCRIPT)
        try:
            return int(await self._redis.evalsha(_script_sha, 1, key, window))  # type: ignore[arg-type]
        except NoScriptError:
            # Redis restarted and lost its script cache: load the script again
            _script_sha = await self._redis.script_load(_INCR_SCRIPT)
            return int(await self._redis.evalsha(_script_sha, 1, key, window))  # type: ignore[arg-type]

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def is_banned(self, ip: str) -> bool:
        """Return True when the IP is in the temporary ban list."""
        try:
            return bool(await self._redis.exists(f"ban:{ip}"))
        except REDIS_ERRORS as exc:
            logger.error("Ban check Redis error — fail-open", extra={"error": str(exc)})
            return False

    async def ban(self, ip: str, duration: int | None = None) -> int:
        """Ban an IP for *duration* seconds (ban_duration_seconds if not given) and return the duration used."""
        dur = duration or self._cfg.ban_duration_seconds
        key = f"ban:{ip}"
        await self._redis.setex(key, dur, "1")
        logger.warning("IP banned", extra={"ip": ip, "duration": dur})
        return dur

    async def unban(self, ip: str) -> bool:
        """Lift a ban before it expires. Returns False if the IP wasn't banned."""
        removed = bool(await self._redis.delete(f"ban:{ip}"))
        if removed:
            logger.warning("IP unbanned", extra={"ip": ip})
        return removed

    async def check(
        self,
        ip: str,
        route_limit: int | None = None,
        route: str = DEFAULT_ROUTE,
    ) -> tuple[bool, int, int]:
        """
        Check & increment the counter for *ip* on *route*.

        *route* is the matched route prefix (e.g. "/users"), not the request
        path: keying on the full path would give every URL its own counter.

        Returns:
            allowed  – True if under the limit
            count    – current request count in the window
            limit    – effective limit that was applied
        """
        if not self._cfg.enabled:
            return True, 0, 0

        limit = self._limit(route_limit)

        try:
            count = await self._incr(self._key(ip, route))
        except REDIS_ERRORS as exc:
            # If Redis is down, fail-open (don't block all traffic)
            logger.error("Rate limiter Redis error — fail-open", extra={"error": str(exc)})
            return True, 0, limit

        allowed = count <= limit
        if not allowed:
            logger.warning(
                "Rate limit exceeded",
                extra={"ip": ip, "count": count, "limit": limit, "route": route},
            )
        return allowed, count, limit

    async def remaining(self, ip: str, route_limit: int | None = None, route: str = DEFAULT_ROUTE) -> int:
        limit = self._limit(route_limit)
        try:
            count_str = await self._redis.get(self._key(ip, route))
        except REDIS_ERRORS:
            return limit  # fail-open: nothing is being counted
        count = int(count_str) if count_str else 0
        return max(0, limit - count)

    async def retry_after(self, ip: str, route: str = DEFAULT_ROUTE) -> int:
        """Seconds until this counter's window ends; the whole window if Redis can't say."""
        try:
            ttl = await self._redis.ttl(self._key(ip, route))
        except REDIS_ERRORS:
            return self._cfg.window_seconds
        return ttl if ttl > 0 else self._cfg.window_seconds

    async def reset(self, ip: str) -> None:
        """Remove this IP's counters on every route (admin / test helper)."""
        await self._redis.delete(*self._route_keys(ip))

    async def get_stats(self, ip: str) -> dict:
        """Counters for each configured route, plus ban status. Not fail-open: admins should see errors."""
        routes = [(r.path, self._limit(r.rate_limit)) for r in settings.routes]
        pipe = self._redis.pipeline()
        for path, _ in routes:
            key = self._key(ip, path)
            pipe.get(key)
            pipe.ttl(key)
        results = await pipe.execute()

        per_route = []
        for i, (path, limit) in enumerate(routes):
            count_str, ttl = results[2 * i], results[2 * i + 1]
            count = int(count_str) if count_str else 0
            per_route.append({
                "route": path,
                "count": count,
                "limit": limit,
                "remaining": max(0, limit - count),
                "reset_in_seconds": max(ttl, 0),
            })

        banned = bool(await self._redis.exists(f"ban:{ip}"))
        ban_ttl = await self._redis.ttl(f"ban:{ip}") if banned else None
        return {
            "ip": ip,
            "banned": banned,
            "ban_expires_in_seconds": ban_ttl,
            "routes": per_route,
        }
