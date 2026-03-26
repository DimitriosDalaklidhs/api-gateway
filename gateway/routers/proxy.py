"""

Core proxy router — handles all traffic forwarding.

Wires together rate limiting, circuit breaking, caching, and auth.

"""



import logging

import time

import uuid

from typing import Optional



import redis.asyncio as aioredis

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status



from core.config import settings, RouteConfig

from core.redis_client import get_redis

from services.auth import optional_auth, require_auth

from services.cache import CacheService

from services.circuit_breaker import CircuitBreakerRegistry, CircuitBreakerOpen

from services.proxy import ProxyService

from services.rate_limiter import RateLimiter



logger = logging.getLogger("gateway.router.proxy")



router = APIRouter()





def _get_client_ip(request: Request) -> str:

    forwarded_for = request.headers.get("X-Forwarded-For")

    if forwarded_for:

        return forwarded_for.split(",")[0].strip()

    return request.client.host if request.client else "0.0.0.0"





def _match_route(path: str) -> Optional[RouteConfig]:

    """

    Longest-prefix match.

    e.g. /users/123 matches the /users route.

    """

    best: Optional[RouteConfig] = None

    best_len = -1

    for route in settings.routes:

        if path == route.path or path.startswith(route.path + "/") or path.startswith(route.path + "?"):

            if len(route.path) > best_len:

                best = route

                best_len = len(route.path)

    return best





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

    request_id = str(uuid.uuid4())

    client_ip = _get_client_ip(request)

    start = time.perf_counter()



    # ── Route matching ──────────────────────────────────────────────────

    route = _match_route(request.url.path)

    if not route:

        return Response(

            content=f'{{"error": "No route configured for {request.url.path}"}}',

            status_code=404,

            media_type="application/json",

        )



    # ── Method check ────────────────────────────────────────────────────

    if request.method not in route.methods:

        return Response(

            content=f'{{"error": "Method {request.method} not allowed on {route.path}"}}',

            status_code=405,

            media_type="application/json",

        )



    # ── Auth ─────────────────────────────────────────────────────────────

    if route.auth_required:

        try:

            await require_auth(request)

        except HTTPException as exc:

            return Response(

                content=f'{{"error": "{exc.detail}"}}',

                status_code=exc.status_code,

                media_type="application/json",

                headers=exc.headers or {},

            )



    # ── Rate limiting ───────────────────────────────────────────────────

    rl = RateLimiter(redis)

    if await rl.is_banned(client_ip):

        return Response(

            content='{"error": "IP temporarily banned"}',

            status_code=429,

            media_type="application/json",

        )



    allowed, count, limit = await rl.check(client_ip, route.rate_limit, request.url.path)

    if not allowed:

        return Response(

            content=f'{{"error": "Rate limit exceeded ({count}/{limit})","retry_after": {settings.rate_limiting.window_seconds}}}',

            status_code=429,

            media_type="application/json",

            headers={

                "X-RateLimit-Limit": str(limit),

                "X-RateLimit-Remaining": "0",

                "Retry-After": str(settings.rate_limiting.window_seconds),

            },

        )



    # ── Cache (GET only) ────────────────────────────────────────────────

    cache = CacheService(redis)

    cached = await cache.get(request)

    if cached is not None:

        cached.headers["X-Request-ID"] = request_id

        return cached



    # ── Circuit breaker ─────────────────────────────────────────────────

    cb_registry = CircuitBreakerRegistry(redis)

    cb = cb_registry.get(route.target)



    # ── Proxy ───────────────────────────────────────────────────────────

    proxy = ProxyService(route=route, circuit_breaker=cb)

    response = await proxy.forward(request, request_id)



    # ── Cache successful GET responses ──────────────────────────────────

    if request.method == "GET" and response.status_code < 400:

        await cache.set(request, response)



    # ── Response headers ────────────────────────────────────────────────

    remaining = await rl.remaining(client_ip, route.rate_limit)

    response.headers["X-RateLimit-Limit"] = str(limit)

    response.headers["X-RateLimit-Remaining"] = str(remaining)

    response.headers["X-Request-ID"] = request_id

    latency = (time.perf_counter() - start) * 1000

    response.headers["X-Response-Time-Ms"] = f"{latency:.2f}"



    return response