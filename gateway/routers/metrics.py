"""
Prometheus scrape endpoint.
  GET /metrics

HTTP metrics (request count, latency histogram, sizes) are recorded by
prometheus-fastapi-instrumentator in main.py. This module adds gateway-specific
gauges that are read from Redis at scrape time, so every replica reports the
same shared circuit breaker state.
"""

import logging

import redis.asyncio as aioredis
from core.config import settings
from core.redis_client import get_redis
from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Gauge, generate_latest
from services.circuit_breaker import CircuitBreakerRegistry, CircuitState

logger = logging.getLogger("gateway.router.metrics")

router = APIRouter()

# Numeric encoding so Grafana can graph state changes over time.
STATE_VALUE = {
    CircuitState.CLOSED: 0,
    CircuitState.HALF_OPEN: 1,
    CircuitState.OPEN: 2,
}

CIRCUIT_STATE = Gauge(
    "gateway_circuit_breaker_state",
    "Circuit breaker state per route: 0=CLOSED, 1=HALF_OPEN, 2=OPEN",
    ["route", "target"],
)
CIRCUIT_FAILURES = Gauge(
    "gateway_circuit_breaker_failures",
    "Consecutive failures counted toward opening the circuit",
    ["route", "target"],
)
REDIS_UP = Gauge(
    "gateway_redis_up",
    "1 if this replica could read breaker state from Redis during the last scrape",
)


async def _refresh_gauges(redis: aioredis.Redis) -> None:
    registry = CircuitBreakerRegistry(redis)
    try:
        for route in settings.routes:
            status = await registry.get(route.target).status()
            labels = {"route": route.path, "target": route.target}
            CIRCUIT_STATE.labels(**labels).set(STATE_VALUE[CircuitState(status["state"])])
            CIRCUIT_FAILURES.labels(**labels).set(status["failures"])
        REDIS_UP.set(1)
    except Exception as exc:
        # Keep serving HTTP metrics even when Redis is down; the gauge shows the outage.
        REDIS_UP.set(0)
        logger.warning("Could not refresh breaker gauges", extra={"error": str(exc)})


@router.get("/metrics", include_in_schema=False)
async def metrics(redis: aioredis.Redis = Depends(get_redis)) -> Response:
    await _refresh_gauges(redis)
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
