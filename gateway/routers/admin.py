"""
Admin / observability endpoints.
  GET  /admin/live
  GET  /admin/health
  GET  /admin/metrics
  GET  /admin/routes
  GET  /admin/circuit-breakers
  POST /admin/circuit-breakers/reset
  POST /admin/circuit-breakers/{service}/reset
  GET  /admin/rate-limit/{ip}
  POST /admin/rate-limit/{ip}/reset
  POST /admin/rate-limit/{ip}/ban
  DELETE /admin/rate-limit/{ip}/ban
  POST /admin/cache/invalidate
  POST /auth/token
"""

import ipaddress
import logging
import time
from typing import Any

import redis.asyncio as aioredis
from core.config import settings
from core.redis_client import get_redis
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from models.schemas import TokenRequest, TokenResponse
from services.auth import create_token
from services.cache import CacheService
from services.circuit_breaker import CircuitBreaker
from services.rate_limiter import RateLimiter
from utils.errors import error_response

logger = logging.getLogger("gateway.router.admin")

router = APIRouter()
_START_TIME = time.time()


def _targets() -> list[str]:
    """Each downstream once. Breakers are per target, and several routes can share one."""
    return sorted({route.target for route in settings.routes})


# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/auth/token", response_model=TokenResponse, tags=["Auth"])
async def get_token(body: TokenRequest) -> TokenResponse:
    """
    Issue a JWT. Demo only: any username and password are accepted, and the
    caller picks its own roles. A real deployment would check both against a
    user store before signing anything.
    """
    token = create_token(
        subject=body.username,
        roles=body.roles or ["user"],
    )
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt.expire_minutes * 60,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/live", tags=["Admin"])
async def live() -> dict[str, str]:
    """
    Liveness. Answers whenever the process can serve a request and touches
    nothing else: a failed liveness probe restarts the pod, and a restart
    fixes nothing that is wrong with Redis.
    """
    return {"status": "alive"}


@router.get("/admin/health", tags=["Admin"])
async def health(redis: aioredis.Redis = Depends(get_redis)) -> dict[str, Any]:
    """
    Readiness and status. Always 200: the gateway serves traffic without Redis
    (it fails open), so "degraded" must not take the pod out of rotation.
    """
    try:
        await redis.ping()
        redis_status = "ok"
    except Exception:
        redis_status = "unreachable"

    return {
        "status": "ok" if redis_status == "ok" else "degraded",
        "redis": redis_status,
        "version": settings.gateway.version,
        "uptime_seconds": round(time.time() - _START_TIME, 1),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/metrics", tags=["Admin"])
async def metrics(redis: aioredis.Redis = Depends(get_redis)) -> dict[str, Any]:
    circuit_statuses = [await CircuitBreaker(redis, target).status() for target in _targets()]

    return {
        "uptime_seconds": round(time.time() - _START_TIME, 1),
        "routes": [
            {
                "path": r.path,
                "target": r.target,
                "methods": r.methods,
                "rate_limit": r.rate_limit if r.rate_limit is not None else settings.rate_limiting.default_limit,
                "auth_required": r.auth_required,
            }
            for r in settings.routes
        ],
        "circuit_breakers": circuit_statuses,
        "config": {
            "rate_limiting": settings.rate_limiting.model_dump(),
            "retry": settings.retry.model_dump(),
            "caching": settings.caching.model_dump(),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/routes", tags=["Admin"])
async def list_routes() -> list[dict[str, Any]]:
    return [r.model_dump() for r in settings.routes]


# ─────────────────────────────────────────────────────────────────────────────
# Circuit Breakers
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/circuit-breakers", tags=["Admin"])
async def circuit_breakers(redis: aioredis.Redis = Depends(get_redis)) -> list[dict[str, Any]]:
    return [await CircuitBreaker(redis, target).status() for target in _targets()]


@router.post("/admin/circuit-breakers/reset", tags=["Admin"])
async def reset_all_circuits(redis: aioredis.Redis = Depends(get_redis)) -> dict[str, str]:
    for target in _targets():
        await CircuitBreaker(redis, target).reset()
    return {"status": "all circuits reset"}


# response_model=None: the return type is a union with a Response, which
# FastAPI can't turn into a response schema
@router.post("/admin/circuit-breakers/{service_key:path}/reset", tags=["Admin"], response_model=None)
async def reset_circuit(service_key: str, redis: aioredis.Redis = Depends(get_redis)) -> JSONResponse | dict[str, str]:
    # service_key is the URL-encoded target. The server decodes %2F to "/"
    # before routing, so it needs the :path converter: a plain {service_key}
    # can't contain slashes, and every target URL has them.
    if service_key not in _targets():
        # Resetting writes a state key, so an unknown name would leave one behind
        return error_response(404, "No route uses that target")
    await CircuitBreaker(redis, service_key).reset()
    return {"status": "reset", "service": service_key}


# ─────────────────────────────────────────────────────────────────────────────
# Rate Limiting
# ─────────────────────────────────────────────────────────────────────────────

# A ban longer than this is a typo, not a policy
_MAX_BAN_SECONDS = 30 * 24 * 3600


def _canonical_ip(ip: str) -> str | None:
    """
    The address in the form the limiter sees it in, or None if it isn't one.
    "::0001" and "::1" are the same client; the ban key must match the second,
    which is how the peer address arrives.
    """
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return None


def _not_an_ip(ip: str) -> JSONResponse:
    return error_response(400, f"Not an IP address: {ip}")


@router.get("/admin/rate-limit/{ip}", tags=["Admin"], response_model=None)
async def rate_limit_stats(ip: str, redis: aioredis.Redis = Depends(get_redis)) -> dict[str, Any] | JSONResponse:
    if (addr := _canonical_ip(ip)) is None:
        return _not_an_ip(ip)
    return await RateLimiter(redis).get_stats(addr)


@router.post("/admin/rate-limit/{ip}/reset", tags=["Admin"], response_model=None)
async def rate_limit_reset(ip: str, redis: aioredis.Redis = Depends(get_redis)) -> dict[str, str] | JSONResponse:
    if (addr := _canonical_ip(ip)) is None:
        return _not_an_ip(ip)
    await RateLimiter(redis).reset(addr)
    return {"status": "reset", "ip": addr}


@router.post("/admin/rate-limit/{ip}/ban", tags=["Admin"], response_model=None)
async def ban_ip(
    ip: str,
    duration: int | None = Query(
        None, ge=1, le=_MAX_BAN_SECONDS,
        description="Seconds, up to 30 days. Defaults to rate_limiting.ban_duration_seconds.",
    ),
    redis: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any] | JSONResponse:
    if (addr := _canonical_ip(ip)) is None:
        return _not_an_ip(ip)
    used = await RateLimiter(redis).ban(addr, duration)
    return {"status": "banned", "ip": addr, "duration_seconds": used}


@router.delete("/admin/rate-limit/{ip}/ban", tags=["Admin"], response_model=None)
async def unban_ip(ip: str, redis: aioredis.Redis = Depends(get_redis)) -> dict[str, Any] | JSONResponse:
    if (addr := _canonical_ip(ip)) is None:
        return _not_an_ip(ip)
    was_banned = await RateLimiter(redis).unban(addr)
    return {"status": "unbanned" if was_banned else "not banned", "ip": addr}


# ─────────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/admin/cache/invalidate", tags=["Admin"])
async def invalidate_cache(
    pattern: str = "*",
    redis: aioredis.Redis = Depends(get_redis),
) -> dict[str, Any]:
    cache = CacheService(redis)
    deleted = await cache.invalidate(pattern)
    return {"status": "invalidated", "keys_deleted": deleted, "pattern": pattern}
