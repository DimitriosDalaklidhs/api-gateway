"""
API Gateway: application entry point.

Builds the FastAPI app, wires the middleware and the routers, and owns the
Redis connection for the lifetime of the process.
"""

import logging
from contextlib import asynccontextmanager

import core.logging  # importing it installs the JSON log handlers
from core.config import settings
from core.redis_client import close_redis, get_redis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from redis.exceptions import RedisError
from routers.admin import router as admin_router
from routers.metrics import router as metrics_router
from routers.proxy import router as proxy_router
from utils.errors import error_response
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
    description=(
        "Reverse-proxy API gateway: path routing, per-route rate limits, retries, "
        "a circuit breaker per downstream, a response cache and JWT checks. "
        "The endpoints listed here are the gateway's own; everything else is proxied."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─────────────────────────────────────────────────────────────────────────────
# Middleware. Starlette wraps in reverse: the LAST add_middleware call is the
# outermost layer, so a request passes Prometheus → Logging → CORS → routes.
# ─────────────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(LoggingMiddleware)

# HTTP metrics (request count, latency, sizes) for Prometheus, served at /metrics.
# Probes and scrapes are excluded so they don't drown out real traffic.
Instrumentator(excluded_handlers=["/metrics", "/admin/health"]).instrument(app)

# ─────────────────────────────────────────────────────────────────────────────
# Routers — admin and metrics first so they are NOT caught by the proxy wildcard
# ─────────────────────────────────────────────────────────────────────────────

app.include_router(admin_router)
app.include_router(metrics_router)
app.include_router(proxy_router)


# ─────────────────────────────────────────────────────────────────────────────
# Global exception handlers. Error details go to the logs, never to the client.
# ─────────────────────────────────────────────────────────────────────────────

@app.exception_handler(RedisError)
async def redis_unavailable_handler(request: Request, exc: RedisError) -> JSONResponse:
    # The proxy path fails open on Redis errors; this catches what's left
    # (admin endpoints that read or write Redis state).
    logger.error("Redis unavailable", extra={"path": request.url.path, "error": str(exc)})
    return error_response(503, "Gateway state store unavailable")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Backstop only. LoggingMiddleware answers errors raised inside it, because
    # this handler runs outside every middleware: a response built here carries
    # no request ID and no CORS headers.
    logger.error(
        "Unhandled exception",
        exc_info=True,
        extra={"path": request.url.path},
    )
    return error_response(500, "Internal gateway error")


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
        proxy_headers=False,  # never trust client-sent X-Forwarded-For (same as the Dockerfile)
    )
