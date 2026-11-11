"""
Circuit breaker (Redis-backed).

States:
  CLOSED    → normal traffic flows through
  OPEN      → requests are short-circuited immediately
  HALF_OPEN → a limited number of probe requests are allowed through

Transitions:
  CLOSED    → OPEN       failure_threshold consecutive failed requests
  OPEN      → HALF_OPEN  recovery_timeout_seconds after opening (checked on
                         the next request, not by a timer)
  HALF_OPEN → CLOSED     a probe succeeds
  HALF_OPEN → OPEN       a probe fails

The proxy calls before_call() once per client request and reports one outcome
per request, so its retries don't count as extra failures or use up probes.

State is stored in Redis so all gateway instances share it. Transitions are
read-then-write, not atomic, so two replicas can race at the moment of a
transition (README, Known limitations). If Redis is unreachable the breaker
fails open: requests go through as if it were CLOSED.
"""

import logging
import time
from enum import Enum

import redis.asyncio as aioredis
from core.config import settings
from core.redis_client import REDIS_ERRORS

logger = logging.getLogger("gateway.circuit_breaker")


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
        level = logging.WARNING if state == CircuitState.OPEN else logging.INFO
        logger.log(
            level,
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
    #  Transitions. Each runs only when the state really changes, so the  #
    #  "Circuit state changed" log line is never noise.                    #
    # ------------------------------------------------------------------ #

    async def _open(self) -> None:
        await self._set_state(CircuitState.OPEN)
        # No TTL: this timestamp has to exist for as long as the circuit is
        # open. With a TTL, a quiet period longer than the TTL left the circuit
        # OPEN with no way to recover. It is deleted when the circuit closes.
        await self._redis.set(self._key_opened, str(time.time()))
        await self._redis.delete(self._key_probes)

    async def _half_open(self) -> None:
        await self._set_state(CircuitState.HALF_OPEN)
        # Fresh probe budget, and old failures no longer count
        await self._redis.delete(self._key_failures, self._key_probes)

    async def _close(self) -> None:
        await self._set_state(CircuitState.CLOSED)
        await self._reset_counters()

    async def _recovery_elapsed(self) -> bool:
        opened_at_str = await self._redis.get(self._key_opened)
        if not opened_at_str:
            # No timestamp (e.g. one an older version let expire): let a probe
            # through rather than stay OPEN forever
            return True
        return time.time() - float(opened_at_str) >= self._cfg.recovery_timeout_seconds

    async def _take_probe(self) -> bool:
        """Allow only a fixed number of probe calls in HALF_OPEN state."""
        probes = await self._redis.incr(self._key_probes)
        if probes == 1:
            # The budget refills every recovery period, so probes lost to
            # cancelled requests can't leave the circuit stuck in HALF_OPEN
            await self._redis.expire(self._key_probes, self._cfg.recovery_timeout_seconds)
        return probes <= self._cfg.half_open_max_calls

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def before_call(self) -> CircuitState:
        """
        Call once before forwarding a client request.

        Returns the state the request goes through under (CLOSED or HALF_OPEN);
        pass it to on_success() / on_failure() when the request finishes.
        Raises CircuitBreakerOpen when traffic should be blocked.
        """
        try:
            state = await self.get_state()

            if state == CircuitState.CLOSED:
                return state

            if state == CircuitState.OPEN:
                if not await self._recovery_elapsed():
                    raise CircuitBreakerOpen(self._service)
                await self._half_open()

            # HALF_OPEN — only let a probe through
            if not await self._take_probe():
                raise CircuitBreakerOpen(self._service)
            return CircuitState.HALF_OPEN

        except REDIS_ERRORS as exc:
            logger.error(
                "Circuit breaker Redis error — fail-open",
                extra={"service": self._service, "error": str(exc)},
            )
            return CircuitState.CLOSED

    async def on_success(self, state: CircuitState | None = None) -> None:
        """Call after a successful downstream response, with the state from before_call()."""
        try:
            if state is None:
                state = await self.get_state()
            if state == CircuitState.CLOSED:
                # Failures have to be consecutive to open the circuit. One
                # delete, no state write and no log line on the hot path.
                await self._redis.delete(self._key_failures)
            elif state == CircuitState.HALF_OPEN:
                await self._close()
        except REDIS_ERRORS as exc:
            logger.error("Circuit breaker Redis error", extra={"service": self._service, "error": str(exc)})

    async def on_failure(self, state: CircuitState | None = None) -> None:
        """Call after a failed downstream request, with the state from before_call()."""
        try:
            if state is None:
                state = await self.get_state()
            if state == CircuitState.HALF_OPEN:
                await self._open()  # a failed probe reopens the circuit
                return
            if state == CircuitState.OPEN:
                return
            # INCR hands every failure a distinct count, so exactly one request
            # crosses the threshold and opens the circuit; failures from
            # requests still in flight don't reopen it and reset the timer.
            if await self._increment_failures() == self._cfg.failure_threshold:
                await self._open()
        except REDIS_ERRORS as exc:
            logger.error("Circuit breaker Redis error", extra={"service": self._service, "error": str(exc)})

    async def reset(self) -> None:
        """Manually reset the circuit (admin / test helper)."""
        await self._close()

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

