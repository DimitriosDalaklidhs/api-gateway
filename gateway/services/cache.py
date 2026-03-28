"""
Redis request/response cache for the API Gateway.

Only caches responses for configurable HTTP methods (default: GET).
Cache keys are derived from a SHA-256 hash of the method + full URL,
ensuring distinct entries per unique request while keeping key lengths
predictable and Redis-safe.

Cache flow:
    Incoming request
        → get()  : return cached Response with X-Cache: HIT header, or None
        → [proxy forwards and gets real response]
        → set()  : store serialised response in Redis with a TTL
        → invalidate() : admin helper to purge keys by pattern
"""

import hashlib
import json
import logging
from typing import Optional

import redis.asyncio as aioredis
from fastapi import Request
from fastapi.responses import Response

from core.config import settings

# Namespaced logger — output appears as "gateway.cache" in log aggregators
logger = logging.getLogger("gateway.cache")


# ══════════════════════════════════════════════════════════════════════════════
# Cache Key
# ══════════════════════════════════════════════════════════════════════════════

def _cache_key(request: Request) -> str:
    """
    Derive a deterministic Redis key for the given request.

    Strategy:
        raw = "<METHOD>:<full URL including query string>"
        key = <cache_prefix> + SHA-256(raw)

    SHA-256 is used to:
      - Keep key length constant (64 hex chars) regardless of URL length
      - Avoid Redis key-size issues with very long query strings
      - Prevent special characters in URLs from corrupting key patterns

    Note: query params are included via request.url (FastAPI preserves their
    original order). If param order varies between clients, consider sorting
    them before hashing to improve cache hit rates.

    Args:
        request: The incoming FastAPI request.

    Returns:
        A prefixed, fixed-length Redis key string.
    """
    # Concatenate method and full URL (includes path + query string)
    raw = f"{request.method}:{request.url}"
    # Prefix scopes the key to this service, making it safe to share a Redis
    # instance with other applications without key collisions
    return settings.caching.cache_prefix + hashlib.sha256(raw.encode()).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
# Cache Service
# ══════════════════════════════════════════════════════════════════════════════

class CacheService:
    """
    Read-through / write-aside response cache backed by Redis.

    The proxy layer calls get() before forwarding a request, and set() after
    receiving a successful upstream response. Error responses (status >= 400)
    are never cached to avoid serving stale error states to future clients.

    All Redis operations are wrapped in try/except so that a Redis outage
    degrades gracefully — the gateway continues to proxy requests without
    caching rather than returning 500 errors.
    """

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis
        self._cfg = settings.caching    # Cache config: enabled, TTL, prefix, cacheable_methods

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _is_cacheable(self, method: str) -> bool:
        """
        Return True if this HTTP method should be cached.

        Checks two conditions:
          1. Caching is globally enabled (settings.caching.enabled)
          2. The method is in the allowed list (default: ["GET"])

        Centralising this check ensures get() and set() apply
        identical eligibility logic without duplicating conditions.

        Args:
            method: HTTP method string (e.g. "GET", "POST").

        Returns:
            True if the request is eligible for caching, False otherwise.
        """
        return (
            self._cfg.enabled
            and method.upper() in self._cfg.cacheable_methods
        )

    # ── Public interface ──────────────────────────────────────────────────────

    async def get(self, request: Request) -> Optional[Response]:
        """
        Attempt to serve the response for this request from cache.

        Flow:
            1. Skip non-cacheable methods immediately (returns None).
            2. Compute the cache key and fetch the raw JSON blob from Redis.
            3. Deserialise the blob back into a FastAPI Response, adding an
               X-Cache: HIT header so clients and observability tools can
               detect cached responses.
            4. Return None on any failure (miss, Redis error, decode error)
               so the proxy falls through to the real upstream.

        Args:
            request: The incoming FastAPI request.

        Returns:
            A reconstructed Response with X-Cache: HIT if a valid cache entry
            exists, or None if the cache should be bypassed.
        """
        # Non-cacheable methods (POST, PUT, etc.) always bypass the cache
        if not self._is_cacheable(request.method):
            return None

        key = _cache_key(request)

        # --- Redis fetch ---
        try:
            raw = await self._redis.get(key)
        except Exception as exc:
            # Redis is unavailable or returned an unexpected error —
            # log and fall through to the upstream rather than failing the request
            logger.warning("Cache GET error", extra={"error": str(exc)})
            return None

        if raw is None:
            return None     # Cache miss — no entry exists for this key

        # --- Deserialise and reconstruct the Response ---
        try:
            data = json.loads(raw)
            logger.debug("Cache hit", extra={"key": key})
            return Response(
                # Content may have been stored as a str or as a list of byte values;
                # handle both to maintain compatibility with different serialisation paths
                content=data["content"].encode() if isinstance(data["content"], str) else bytes(data["content"]),
                status_code=data["status_code"],
                headers={**data["headers"], "X-Cache": "HIT"},  # Signal to clients that this is a cached response
                media_type=data.get("media_type"),
            )
        except Exception as exc:
            # Corrupt or schema-mismatched cache entry — discard and fetch fresh
            logger.warning("Cache decode error", extra={"error": str(exc)})
            return None

    async def set(
        self,
        request: Request,
        response: Response,
        ttl: Optional[int] = None,
    ) -> None:
        """
        Serialise and store a successful upstream response in Redis.

        Only stores the response if:
          - The HTTP method is cacheable (e.g. GET)
          - The response status code is < 400 (errors are never cached)

        The response body is stored as a UTF-8 string. Non-UTF-8 bytes are
        replaced with the replacement character (errors="replace") to ensure
        the JSON payload always serialises cleanly.

        Args:
            request:  The original incoming request (used to derive the cache key).
            response: The upstream response to cache.
            ttl:      TTL in seconds. Falls back to settings.caching.default_ttl_seconds
                      if not provided.
        """
        # Skip non-cacheable methods (POST, DELETE, etc.)
        if not self._is_cacheable(request.method):
            return

        # Never cache error responses — avoids propagating upstream failures
        # to future requests after the upstream has recovered
        if response.status_code >= 400:
            return

        key = _cache_key(request)

        # Serialise all fields needed to reconstruct a faithful Response on cache hit
        payload = {
            "status_code": response.status_code,
            "headers":     dict(response.headers),
            # Decode bytes to str for JSON compatibility; errors="replace" ensures
            # binary or malformed responses don't cause a serialisation failure
            "content":     response.body.decode("utf-8", errors="replace") if response.body else "",
            "media_type":  response.media_type,
        }

        try:
            # setex atomically stores the value and sets its expiry,
            # preventing orphaned keys that never expire
            await self._redis.setex(
                key,
                ttl or self._cfg.default_ttl_seconds,
                json.dumps(payload),
            )
            logger.debug("Cache SET", extra={"key": key, "ttl": ttl or self._cfg.default_ttl_seconds})
        except Exception as exc:
            # Redis write failure is non-fatal — the response has already been
            # returned to the client; we simply won't cache it this time
            logger.warning("Cache SET error", extra={"error": str(exc)})

    async def invalidate(self, pattern: str) -> int:
        """
        Delete all cache keys whose suffix matches *pattern* (admin helper).

        The cache prefix is prepended automatically, so callers pass only the
        meaningful part of the pattern (e.g. "*" to flush the entire cache,
        or "/api/books*" to invalidate all book-related entries).

        Args:
            pattern: Key suffix pattern using Redis glob syntax (* and ?).

        Returns:
            The number of keys deleted (0 if no keys matched).
        """
        # Prepend the cache prefix so the scan is scoped to this service's keys
        keys = await self._redis.keys(self._cfg.cache_prefix + pattern)
        if keys:
            # Unpack all matched keys into a single DELETE call for efficiency
            return await self._redis.delete(*keys)
        return 0    # No matching keys found — nothing to delete
