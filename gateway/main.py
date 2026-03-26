"""
API Gateway — entry point.

Phases implemented:
  1 ✅ Basic Proxy (GET/POST/PUT/DELETE/PATCH + dynamic routing)
  2 ✅ Logging Middleware (JSON structured logs → file + stdout)
  3 ✅ Rate Limiting (Redis sliding-window, per-IP, per-route limits)
  4 ✅ Retries (httpx, exponential backoff, configurable status codes)
  5 ✅ Circuit Breaker (CLOSED/OPEN/HALF-OPEN, Redis state)
  6 ✅ Config System (YAML + env-var overrides)
Bonus:
  ✅ JWT Authentication
  ✅ Request Caching (Redis)
  ✅ /admin/metrics endpoint
  ✅ Admin control plane (ban IPs, reset circuits, invalidate cache)
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core.config import settings
from core.redis_client import close_redis, get_redis
from routers.admin import router as admin_router
from routers.proxy import router as proxy_router
from utils.middleware import LoggingMiddleware

logger = logging.getLogger("gateway")


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Gateway starting",
        extra={
            "version": settings.gateway.version,
            "routes": len(settings.routes),
        },
    )
    # Warm up Redis connection
    try:
        redis = await get_redis()
        await redis.ping()
        logger.info("Redis connection verified")
    except Exception as exc:
        logger.warning("Redis unavailable at startup — some features degraded", extra={"error": str(exc)})

    yield

    logger.info("Gateway shutting down")
    await close_redis()


# ─────────────────────────────────────────────────────────────────────────────
# Application
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.gateway.title,
    version=settings.gateway.version,
    description="""
## High-Performance API Gateway

Built with **FastAPI** + **httpx** + **Redis**.

### Features
| Feature | Status |
|---|---|
| Dynamic proxy routing | ✅ |
| Rate limiting (Redis) | ✅ |
| Retry with backoff | ✅ |
| Circuit breaker | ✅ |
| JWT authentication | ✅ |
| Response caching | ✅ |
| Structured JSON logging | ✅ |
| Admin control plane | ✅ |

### Quick Start
1. `POST /auth/token` with `{"username":"x","password":"y"}` to get a JWT
2. Use the token as `Authorization: Bearer <token>` on protected routes
3. Watch `/admin/metrics` for live circuit breaker and route stats
    """,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─────────────────────────────────────────────────────────────────────────────
# Middleware (outermost registered = outermost in chain)
# ─────────────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(LoggingMiddleware)

# ─────────────────────────────────────────────────────────────────────────────
# Routers — admin first so /admin/* paths are NOT caught by the proxy wildcard
# ─────────────────────────────────────────────────────────────────────────────

app.include_router(admin_router)
app.include_router(proxy_router)


# ─────────────────────────────────────────────────────────────────────────────
# Global exception handler
# ─────────────────────────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Unhandled exception",
        exc_info=True,
        extra={"path": request.url.path},
    )
    return JSONResponse(
        status_code=500,
        content={"error": "Internal gateway error", "detail": str(exc)},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dev runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.gateway.host,
        port=settings.gateway.port,
        reload=settings.gateway.debug,
        log_config=None,  # We handle logging ourselves
    )