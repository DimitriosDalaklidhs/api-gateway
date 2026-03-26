"""
Phase 5 — Circuit Breaker (Redis-backed).

States:
  CLOSED    → normal traffic flows through
  OPEN      → requests are short-circuited immediately
  HALF_OPEN → a limited probe request is allowed through

State is stored in Redis so all gateway instances share it.
"""

import asyncio
import logging
import time
from enum import Enum
from typing import Callable, Optional, TypeVar

import redis.asyncio as aioredis

from core.config import settings

logger = logging.getLogger("gateway.circuit_breaker")

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpen(Exception):
    """Raised when a call is attempted against an OPEN circuit."""
    def __init__(self, service: str) -> None:
        super().__init__(f"Circuit OPEN for service: {service}")
        self.service = service


class CircuitBreaker:
    """
    One CircuitBreaker instance per downstream service target URL.
    All state lives in Redis so it is shared across gateway replicas.
    """

    def __init__(self, redis: aioredis.Redis, service: str) -> None:
        self._redis = redis
        self._service = service
        self._cfg = settings.circuit_breaker

        # Redis keys
        self._key_state    = f"cb:{service}:state"
        self._key_failures = f"cb:{service}:failures"
        self._key_opened   = f"cb:{service}:opened_at"
        self._key_probes   = f"cb:{service}:probes"

    # ------------------------------------------------------------------ #
    #  State helpers                                                       #
    # ------------------------------------------------------------------ #

    async def get_state(self) -> CircuitState:
        raw = await self._redis.get(self._key_state)
        if raw is None:
            return CircuitState.CLOSED
        return CircuitState(raw)

    async def _set_state(self, state: CircuitState) -> None:
        await self._redis.set(self._key_state, state.value)
        logger.info(
            "Circuit state changed",
            extra={"service": self._service, "state": state.value},
        )

    async def _failures(self) -> int:
        val = await self._redis.get(self._key_failures)
        return int(val) if val else 0

    async def _increment_failures(self) -> int:
        count = await self._redis.incr(self._key_failures)
        # Keep failures key alive for twice the recovery window
        await self._redis.expire(
            self._key_failures,
            self._cfg.recovery_timeout_seconds * 2,
        )
        return count

    async def _reset_counters(self) -> None:
        await self._redis.delete(
            self._key_failures, self._key_opened, self._key_probes
        )

    # ------------------------------------------------------------------ #
    #  Transition logic                                                    #
    # ------------------------------------------------------------------ #

    async def _maybe_open(self) -> None:
        failures = await self._increment_failures()
        if failures >= self._cfg.failure_threshold:
            await self._set_state(CircuitState.OPEN)
            await self._redis.set(self._key_opened, str(time.time()))
            await self._redis.expire(
                self._key_opened, self._cfg.recovery_timeout_seconds * 4
            )

    async def _check_recovery(self) -> CircuitState:
        """If enough time has passed since OPEN, transition to HALF_OPEN."""
        opened_at_str = await self._redis.get(self._key_opened)
        if not opened_at_str:
            # Fallback: open it fresh
            return CircuitState.OPEN

        opened_at = float(opened_at_str)
        elapsed = time.time() - opened_at

        if elapsed >= self._cfg.recovery_timeout_seconds:
            await self._set_state(CircuitState.HALF_OPEN)
            await self._redis.set(self._key_probes, "0")
            return CircuitState.HALF_OPEN

        return CircuitState.OPEN

    async def _half_open_probe_allowed(self) -> bool:
        """Allow only a fixed number of probe calls in HALF_OPEN state."""
        probes = await self._redis.incr(self._key_probes)
        return probes <= self._cfg.half_open_max_calls

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def before_call(self) -> None:
        """
        Call before forwarding a request.
        Raises CircuitBreakerOpen when traffic should be blocked.
        """
        state = await self.get_state()

        if state == CircuitState.CLOSED:
            return

        if state == CircuitState.OPEN:
            state = await self._check_recovery()
            if state == CircuitState.OPEN:
                raise CircuitBreakerOpen(self._service)

        # HALF_OPEN — only let a probe through
        if not await self._half_open_probe_allowed():
            raise CircuitBreakerOpen(self._service)

    async def on_success(self) -> None:
        """Call after a successful downstream response."""
        state = await self.get_state()
        if state in (CircuitState.HALF_OPEN, CircuitState.CLOSED):
            await self._set_state(CircuitState.CLOSED)
            await self._reset_counters()

    async def on_failure(self) -> None:
        """Call after a failed downstream response."""
        await self._maybe_open()

    async def reset(self) -> None:
        """Manually reset the circuit (admin / test helper)."""
        await self._set_state(CircuitState.CLOSED)
        await self._reset_counters()

    async def status(self) -> dict:
        state = await self.get_state()
        failures = await self._failures()
        opened_at_str = await self._redis.get(self._key_opened)
        return {
            "service": self._service,
            "state": state.value,
            "failures": failures,
            "threshold": self._cfg.failure_threshold,
            "opened_at": float(opened_at_str) if opened_at_str else None,
        }


class CircuitBreakerRegistry:
    """Lazily creates one CircuitBreaker per service key."""

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, service: str) -> CircuitBreaker:
        if service not in self._breakers:
            self._breakers[service] = CircuitBreaker(self._redis, service)
        return self._breakers[service]

    def all_services(self) -> list[str]:
        return list(self._breakers.keys())