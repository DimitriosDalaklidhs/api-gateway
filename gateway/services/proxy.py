"""
Phase 1 + 4 — Async Proxy with Retry Logic.

Forwards requests to downstream services using httpx.
Implements exponential-backoff retries on timeout or 5xx responses, for
idempotent methods only.
"""

import asyncio
import logging

import httpx
from core.config import RouteConfig, settings
from fastapi import Request
from fastapi.responses import Response
from utils.errors import error_response

from services.circuit_breaker import CircuitBreaker, CircuitBreakerOpen, CircuitState

logger = logging.getLogger("gateway.proxy")

# Hop-by-hop headers that must NOT be forwarded
_HOP_BY_HOP = frozenset(
    {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade",
        "host",  # always rewritten
    }
)

# httpx hands us the body already decompressed, so the upstream's encoding and
# length no longer describe it. Starlette recomputes content-length.
_DECODED_BODY = frozenset({"content-encoding", "content-length"})

# Safe to send twice: repeating them leaves the server in the same state.
# POST and PATCH are not: a retried POST can create the same order twice.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

# Errors where the connection was never made, so the upstream never saw the
# request: safe to retry whatever the method.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout)


def _filter_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def _filter_response_headers(headers: dict) -> dict:
    return {k: v for k, v in _filter_headers(headers).items() if k.lower() not in _DECODED_BODY}


def _build_target_url(request: Request, route: RouteConfig) -> str:
    path = request.url.path
    if route.strip_prefix:
        path = path[len(route.path):]  # remove the matched prefix
        if not path.startswith("/"):
            path = "/" + path

    qs = request.url.query
    url = route.target.rstrip("/") + path
    if qs:
        url += f"?{qs}"
    return url


class ProxyService:
    def __init__(
        self,
        route: RouteConfig,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self._route = route
        self._cb = circuit_breaker
        self._cfg = settings.retry
        self._proxy_cfg = settings.proxy
        self._timeout = httpx.Timeout(
            connect=self._proxy_cfg.connect_timeout_seconds,
            read=self._proxy_cfg.timeout_seconds,
            write=self._proxy_cfg.timeout_seconds,
            pool=self._proxy_cfg.timeout_seconds,
        )

    async def _record(self, cb_state: CircuitState, success: bool) -> None:
        """Report one outcome per client request to the circuit breaker."""
        if not self._cb:
            return
        if success:
            await self._cb.on_success(cb_state)
        else:
            await self._cb.on_failure(cb_state)

    async def forward(self, request: Request, request_id: str) -> Response:
        """
        Forward *request* to the target service.

        Retries up to max_attempts on timeout or retryable status codes, but
        only for idempotent methods. Any method is retried when the connection
        was never made. The circuit breaker is consulted once per client
        request and told one outcome, however many attempts that took.
        """
        target_url = _build_target_url(request, self._route)
        body = await request.body()
        headers = _filter_headers(dict(request.headers))
        # Incoming header names are lowercase; differently-cased keys would be sent
        # alongside the client's own values instead of replacing them.
        headers["x-request-id"] = request_id
        headers["x-forwarded-for"] = request.client.host if request.client else "unknown"
        headers["x-gateway"] = "FastAPI-Gateway/1.0"

        # Circuit breaker guard: once per client request, not per attempt
        cb_state = CircuitState.CLOSED
        if self._cb:
            try:
                cb_state = await self._cb.before_call()
            except CircuitBreakerOpen:
                # Which downstream it is stays in the breaker's own log line:
                # the target is an internal hostname the client has no use for
                return error_response(503, "Service unavailable (circuit open)")

        idempotent = request.method.upper() in _IDEMPOTENT_METHODS
        max_attempts = self._cfg.max_attempts
        resp: httpx.Response | None = None
        last_exc: Exception | None = None
        attempts = 0

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for attempt in range(max_attempts):
                if attempt:
                    sleep_secs = self._cfg.backoff_factor * (2 ** (attempt - 1))
                    logger.info(
                        "Retry attempt",
                        extra={
                            "attempt": attempt + 1,
                            "target": target_url,
                            "sleep": sleep_secs,
                            "request_id": request_id,
                        },
                    )
                    await asyncio.sleep(sleep_secs)

                attempts += 1
                is_last = attempt == max_attempts - 1

                try:
                    resp = await client.request(
                        method=request.method,
                        url=target_url,
                        headers=headers,
                        content=body,
                    )
                except httpx.RequestError as exc:
                    resp = None
                    last_exc = exc
                    logger.warning(
                        "Request timeout" if isinstance(exc, httpx.TimeoutException) else "Request error",
                        extra={
                            "attempt": attempt + 1,
                            "target": target_url,
                            "error": str(exc),
                            "request_id": request_id,
                        },
                    )
                    if is_last or not (idempotent or isinstance(exc, _NEVER_SENT)):
                        break
                    continue

                if resp.status_code in self._cfg.retry_on_status and idempotent and not is_last:
                    continue
                break

        if resp is None:
            await self._record(cb_state, success=False)
            logger.error(
                "Upstream request failed",
                extra={
                    "target": target_url,
                    "attempts": attempts,
                    "error": str(last_exc),
                    "request_id": request_id,
                },
            )
            return error_response(502, f"Upstream unavailable after {attempts} attempt(s)")

        await self._record(cb_state, success=resp.status_code < 500)

        response_headers = _filter_response_headers(dict(resp.headers))
        response_headers["x-request-id"] = request_id

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=response_headers,
            media_type=resp.headers.get("content-type"),
        )
