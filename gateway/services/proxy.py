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

# Request headers the gateway sets itself, replacing whatever the client sent.
# accept-encoding is here because httpx advertises the encodings it can decode:
# passing on the client's own list lets the upstream answer in one httpx can't
# (br, zstd), and that body would reach the client still compressed but, with
# content-encoding dropped below, no longer labelled as such.
_SET_BY_GATEWAY = frozenset({"x-request-id", "x-forwarded-for", "x-gateway", "accept-encoding"})

# httpx hands us the body already decompressed, so the upstream's encoding and
# length no longer describe it. Starlette recomputes content-length.
_DECODED_BODY = frozenset({"content-encoding", "content-length"})

# Safe to send twice: repeating them leaves the server in the same state.
# POST and PATCH are not: a retried POST can create the same order twice.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

# Errors where the connection was never made, so the upstream never saw the
# request: safe to retry whatever the method.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout)


class UnforwardablePath(ValueError):
    """The request path can't be passed upstream as the client sent it."""


def _build_target_url(request: Request, route: RouteConfig) -> httpx.URL:
    """
    The upstream URL for this request.

    The path is forwarded as the client sent it (ASGI's raw_path), not rebuilt
    from the decoded one the route was matched on. Decoding and re-encoding
    turns an escaped "%2F" or "%3F" into a real separator, so the upstream
    would be asked for a different path from the one the client named.
    """
    scope = request.scope
    try:
        path = scope["raw_path"].decode("ascii")
        query = scope.get("query_string", b"").decode("ascii")
    except UnicodeDecodeError as exc:
        raise UnforwardablePath("not ASCII") from exc

    # The route matched the decoded path. If the raw one doesn't start with the
    # same prefix, part of it was percent-encoded ("/%75sers"), and stripping
    # it by length would cut in the wrong place.
    if path != route.path and not path.startswith(route.path + "/"):
        raise UnforwardablePath("route prefix is percent-encoded")
    if route.strip_prefix:
        path = path[len(route.path):] or "/"

    url = route.target.rstrip("/") + path
    if query:
        url += f"?{query}"
    try:
        return httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise UnforwardablePath(str(exc)) from exc


def _upstream_headers(request: Request, request_id: str) -> list[tuple[str, str]]:
    """The client's headers minus hop-by-hop ones, plus the gateway's own."""
    # A list of pairs, not a dict: a dict would keep only the last of a header
    # the client sent more than once
    headers = [
        (name, value)
        for name, value in request.headers.items()
        if name.lower() not in _HOP_BY_HOP and name.lower() not in _SET_BY_GATEWAY
    ]
    headers.append(("x-request-id", request_id))
    # The peer address, never the client's own X-Forwarded-For
    headers.append(("x-forwarded-for", request.client.host if request.client else "unknown"))
    headers.append(("x-gateway", "FastAPI-Gateway/1.0"))
    return headers


def _client_response(upstream: httpx.Response, request_id: str) -> Response:
    response = Response(content=upstream.content, status_code=upstream.status_code)
    # Appended one by one for the same reason: several Set-Cookie headers have
    # to arrive as several headers
    for name, value in upstream.headers.multi_items():
        if name.lower() not in _HOP_BY_HOP and name.lower() not in _DECODED_BODY:
            response.headers.append(name, value)
    response.headers["x-request-id"] = request_id
    return response


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
        # Before the breaker: a path we refuse must not use up a HALF_OPEN probe
        try:
            target_url = _build_target_url(request, self._route)
        except UnforwardablePath as exc:
            logger.info("Rejected request path", extra={"reason": str(exc), "request_id": request_id})
            return error_response(400, "Malformed request path")

        body = await request.body()
        headers = _upstream_headers(request, request_id)

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
                            "target": str(target_url),
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
                            "target": str(target_url),
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
                    "target": str(target_url),
                    "attempts": attempts,
                    "error": str(last_exc),
                    "request_id": request_id,
                },
            )
            return error_response(502, f"Upstream unavailable after {attempts} attempt(s)")

        await self._record(cb_state, success=resp.status_code < 500)
        return _client_response(resp, request_id)
