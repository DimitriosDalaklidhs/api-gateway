"""
Core proxy router — handles all traffic forwarding.
Wires together rate limiting, circuit breaking, caching, and auth.
"""

import logging
import time

import redis.asyncio as aioredis
from core.config import RouteConfig, settings
from core.redis_client import get_redis
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from services.auth import require_auth
from services.cache import CacheService
from services.circuit_breaker import CircuitBreaker
from services.proxy import ProxyService
from services.rate_limiter import RateLimiter
from utils.errors import error_response

logger = logging.getLogger("gateway.router.proxy")

router = APIRouter()


def _match_route(path: str) -> RouteConfig | None:
    """
    Longest-prefix match on whole segments.
    e.g. /users/123 matches the /users route; /usersX does not.
    """
    best: RouteConfig | None = None
    for route in settings.routes:
        if path == route.path or path.startswith(route.path + "/"):
            if best is None or len(route.path) > len(best.path):
                best = route
    return best


def _has_dot_segment(path: str) -> bool:
    return any(segment in (".", "..") for segment in path.split("/"))


# ─────────────────────────────────────────────────────────────────────────────
# Catch-all endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def gateway_proxy(
    request: Request,
    full_path: str,
    redis: aioredis.Redis = Depends(get_redis),
) -> Response:
    # Both set by LoggingMiddleware, so logs, downstream and response agree
    request_id = request.state.request_id
    client_ip = request.state.client_ip
    start = time.perf_counter()

    # The server has percent-decoded this already, so "%2e%2e" arrives as ".."
    path = request.scope["path"]

    # ── Path hygiene ────────────────────────────────────────────────────
    # A route's limit, methods and auth are chosen from the path as sent, but
    # an HTTP client collapses "/users/../orders" to "/orders" before sending
    # it upstream. Refused here, or one route's policy guards another's resource.
    if _has_dot_segment(path):
        return error_response(400, "Path must not contain '.' or '..' segments")

    # ── Route matching ──────────────────────────────────────────────────
    route = _match_route(path)
    if not route:
        return error_response(404, f"No route configured for {path}")

    # ── Method check ────────────────────────────────────────────────────
    if request.method not in route.methods:
        return error_response(405, f"Method {request.method} not allowed on {route.path}")

    # ── Auth ─────────────────────────────────────────────────────────────
    if route.auth_required:
        try:
            await require_auth(request)
        except HTTPException as exc:
            return error_response(exc.status_code, exc.detail, headers=exc.headers)

    # ── Rate limiting ───────────────────────────────────────────────────
    rl = RateLimiter(redis)
    if await rl.is_banned(client_ip):
        return error_response(429, "IP temporarily banned")

    # Keyed on the route prefix (e.g. "/users"), not the request path, so
    # /users/1 and /users/2 share the /users limit
    allowed, count, limit = await rl.check(client_ip, route.rate_limit, route.path)
    if not allowed:
        retry_after = settings.rate_limiting.window_seconds
        return error_response(
            429,
            f"Rate limit exceeded ({count}/{limit})",
            retry_after=retry_after,
            headers={
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "Retry-After": str(retry_after),
            },
        )

    # ── Cache (GET only) ────────────────────────────────────────────────
    cache = CacheService(redis)
    cached = await cache.get(request)
    if cached is not None:
        cached.headers["X-Request-ID"] = request_id
        return cached

    # ── Circuit breaker ─────────────────────────────────────────────────
    cb = CircuitBreaker(redis, route.target)

    # ── Proxy ───────────────────────────────────────────────────────────
    proxy = ProxyService(route=route, circuit_breaker=cb)
    response = await proxy.forward(request, request_id)

    # ── Cache successful GET responses ──────────────────────────────────
    if request.method == "GET" and response.status_code < 400:
        await cache.set(request, response)

    # ── Response headers ────────────────────────────────────────────────
    remaining = await rl.remaining(client_ip, route.rate_limit, route.path)
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-Request-ID"] = request_id
    latency = (time.perf_counter() - start) * 1000
    response.headers["X-Response-Time-Ms"] = f"{latency:.2f}"

    return response
