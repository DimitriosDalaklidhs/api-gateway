"""
Redis response cache.

Stores successful responses to cacheable methods (GET by default) for
default_ttl_seconds. An entry is keyed on the method, the path and query as the
client sent them, and the caller's credentials (Authorization and Cookie), so
one caller's response is never served to another. Callers with no credentials
share entries.

A response is not stored when it is an error, when the upstream marks it as not
for sharing (Set-Cookie, or Cache-Control: private, no-store or no-cache), or
when its body is larger than _MAX_BODY_BYTES.

Any Redis error counts as a miss: the request goes upstream as if there were no
cache.
"""

import base64
import hashlib
import json
import logging

import redis.asyncio as aioredis
from core.config import settings
from fastapi import Request
from fastapi.responses import Response

logger = logging.getLogger("gateway.cache")

# Larger bodies are passed through uncached. Redis here is sized for counters
# and small JSON responses; one big download requested with different query
# strings would fill it.
_MAX_BODY_BYTES = 1_048_576

# Cache-Control directives that rule out a shared cache. no-cache does allow
# storing, but only with revalidation on every use, which this cache can't do.
_NOT_SHAREABLE = ("private", "no-store", "no-cache")


def _cache_key(request: Request) -> str:
    """
    <prefix><decoded path>:<SHA-256 of method, raw path, query and credentials>

    The readable path in front is what the admin API's invalidation patterns
    match ("/users*"). The hash covers the request as it was sent: the raw path
    and query, so "/a%3Fb" and "/a?b" are two entries, and both credential
    headers, so a response that depends on who is asking is stored per caller.
    """
    scope = request.scope
    parts = [
        request.method.encode(),
        scope.get("raw_path") or scope["path"].encode(),
        scope.get("query_string", b""),
        request.headers.get("authorization", "").encode(),
        request.headers.get("cookie", "").encode(),
    ]
    digest = hashlib.sha256(b"\n".join(parts)).hexdigest()
    return f"{settings.caching.cache_prefix}{scope['path']}:{digest}"


class CacheService:
    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis
        self._cfg = settings.caching

    def _is_cacheable(self, method: str) -> bool:
        return self._cfg.enabled and method.upper() in self._cfg.cacheable_methods

    @staticmethod
    def _is_storable(response: Response) -> bool:
        if response.status_code >= 400 or len(response.body) > _MAX_BODY_BYTES:
            return False
        # A Set-Cookie belongs to the caller it was issued to
        if "set-cookie" in response.headers:
            return False
        cache_control = response.headers.get("cache-control", "").lower()
        return not any(directive in cache_control for directive in _NOT_SHAREABLE)

    async def get(self, request: Request) -> Response | None:
        """The stored response, marked X-Cache: HIT, or None on a miss."""
        if not self._is_cacheable(request.method):
            return None
        try:
            raw = await self._redis.get(_cache_key(request))
            if raw is None:
                return None
            entry = json.loads(raw)
            response = Response(content=base64.b64decode(entry["body"]), status_code=entry["status"])
            for name, value in entry["headers"]:
                response.headers.append(name, value)
        except Exception as exc:
            # Redis is down, or the entry was written in another format (an
            # older replica during a rollout). The upstream has the answer.
            logger.warning("Cache read failed", extra={"error": str(exc)})
            return None
        response.headers["X-Cache"] = "HIT"
        return response

    async def set(self, request: Request, response: Response, ttl: int | None = None) -> None:
        """Store *response* if the method, the status and the upstream's own headers allow it."""
        if not self._is_cacheable(request.method) or not self._is_storable(response):
            return
        entry = {
            "status": response.status_code,
            # Pairs, so repeated headers survive. content-length is left out
            # because Starlette sets it from the body when the entry is served.
            "headers": [[name, value] for name, value in response.headers.items() if name.lower() != "content-length"],
            # JSON can't hold bytes. Decoding the body as text instead changed
            # every byte that wasn't valid UTF-8, so binary responses came back
            # corrupted and longer than their Content-Length.
            "body": base64.b64encode(response.body).decode("ascii"),
        }
        try:
            # SETEX stores the value and its expiry in one command
            await self._redis.setex(_cache_key(request), ttl or self._cfg.default_ttl_seconds, json.dumps(entry))
        except Exception as exc:
            # The client already has its response; it just isn't cached
            logger.warning("Cache write failed", extra={"error": str(exc)})

    async def invalidate(self, pattern: str) -> int:
        """
        Delete entries whose path matches *pattern* (Redis glob syntax) and
        return how many there were. "*" clears the cache, "/users*" clears
        everything under /users.
        """
        # SCAN walks the keyspace in small batches. KEYS would block Redis,
        # and with it every request's rate-limit check, until it finished.
        keys = [k async for k in self._redis.scan_iter(match=self._cfg.cache_prefix + pattern, count=500)]
        if keys:
            return await self._redis.delete(*keys)
        return 0
