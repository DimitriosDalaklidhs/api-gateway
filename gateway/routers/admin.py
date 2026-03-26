"""
Admin / observability endpoints.
  GET  /admin/health
  GET  /admin/metrics
  GET  /admin/routes
  GET  /admin/circuit-breakers
  POST /admin/circuit-breakers/{service}/reset
  GET  /admin/rate-limit/{ip}
  POST /admin/rate-limit/{ip}/reset
  POST /admin/rate-limit/{ip}/ban
  POST /admin/cache/invalidate
  POST /auth/token
"""

import time
import logging
from typing import Any, Dict, List

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from core.config import settings
from core.redis_client import get_redis
from models.schemas import TokenRequest, TokenResponse
from services.auth import create_token, require_auth
from services.cache import CacheService
from services.circuit_breaker import CircuitBreakerRegistry
from services.rate_limiter import RateLimiter

logger = logging.getLogger("gateway.router.admin")

router = APIRouter()
_START_TIME = time.time()

# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/auth/token", response_model=TokenResponse, tags=["Auth"])
async def get_token(body: TokenRequest) -> TokenResponse:
    """
    Issue a JWT. In production, validate credentials against a user store.
    This demo accepts any username/password.
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

@router.get("/admin/health", tags=["Admin"])
async def health(redis: aioredis.Redis = Depends(get_redis)) -> Dict[str, Any]:
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
async def metrics(redis: aioredis.Redis = Depends(get_redis)) -> Dict[str, Any]:
    cb_registry = CircuitBreakerRegistry(redis)
    circuit_statuses = []
    for route in settings.routes:
        cb = cb_registry.get(route.target)
        circuit_statuses.append(await cb.status())

    return {
        "uptime_seconds": round(time.time() - _START_TIME, 1),
        "routes": [
            {
                "path": r.path,
                "target": r.target,
                "methods": r.methods,
                "rate_limit": r.rate_limit,
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
async def list_routes() -> List[Dict[str, Any]]:
    return [r.model_dump() for r in settings.routes]


# ─────────────────────────────────────────────────────────────────────────────
# Circuit Breakers
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/circuit-breakers", tags=["Admin"])
async def circuit_breakers(redis: aioredis.Redis = Depends(get_redis)) -> List[Dict[str, Any]]:
    registry = CircuitBreakerRegistry(redis)
    results = []
    for route in settings.routes:
        cb = registry.get(route.target)
        results.append(await cb.status())
    return results


@router.post("/admin/circuit-breakers/reset", tags=["Admin"])
async def reset_all_circuits(redis: aioredis.Redis = Depends(get_redis)) -> Dict[str, str]:
    registry = CircuitBreakerRegistry(redis)
    for route in settings.routes:
        cb = registry.get(route.target)
        await cb.reset()
    return {"status": "all circuits reset"}


@router.post("/admin/circuit-breakers/{service_key}/reset", tags=["Admin"])
async def reset_circuit(service_key: str, redis: aioredis.Redis = Depends(get_redis)) -> Dict[str, str]:
    # service_key is URL-encoded target
    import urllib.parse
    target = urllib.parse.unquote(service_key)
    registry = CircuitBreakerRegistry(redis)
    cb = registry.get(target)
    await cb.reset()
    return {"status": "reset", "service": target}


# ─────────────────────────────────────────────────────────────────────────────
# Rate Limiting
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/rate-limit/{ip}", tags=["Admin"])
async def rate_limit_stats(ip: str, redis: aioredis.Redis = Depends(get_redis)) -> Dict[str, Any]:
    rl = RateLimiter(redis)
    return await rl.get_stats(ip)


@router.post("/admin/rate-limit/{ip}/reset", tags=["Admin"])
async def rate_limit_reset(ip: str, redis: aioredis.Redis = Depends(get_redis)) -> Dict[str, str]:
    rl = RateLimiter(redis)
    await rl.reset(ip)
    return {"status": "reset", "ip": ip}


@router.post("/admin/rate-limit/{ip}/ban", tags=["Admin"])
async def ban_ip(
    ip: str,
    duration: int = 300,
    redis: aioredis.Redis = Depends(get_redis),
) -> Dict[str, Any]:
    rl = RateLimiter(redis)
    await rl.ban(ip, duration)
    return {"status": "banned", "ip": ip, "duration_seconds": duration}


# ─────────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/admin/cache/invalidate", tags=["Admin"])
async def invalidate_cache(
    pattern: str = "*",
    redis: aioredis.Redis = Depends(get_redis),
) -> Dict[str, Any]:
    cache = CacheService(redis)
    deleted = await cache.invalidate(pattern)
    return {"status": "invalidated", "keys_deleted": deleted, "pattern": pattern}