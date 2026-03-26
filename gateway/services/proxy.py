"""

Phase 1 + 4 — Async Proxy with Retry Logic.



Forwards requests to downstream services using httpx.

Implements exponential-backoff retries on timeout or 5xx responses.

"""



import asyncio

import logging

import time

from typing import Optional



import httpx

from fastapi import Request

from fastapi.responses import Response



from core.config import settings, RouteConfig

from services.circuit_breaker import CircuitBreaker, CircuitBreakerOpen



logger = logging.getLogger("gateway.proxy")



# Hop-by-hop headers that must NOT be forwarded

_HOP_BY_HOP = frozenset(

    {

        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",

        "te", "trailers", "transfer-encoding", "upgrade",

        "host",  # always rewritten

    }

)





def _filter_headers(headers: dict) -> dict:

    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}





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

        circuit_breaker: Optional[CircuitBreaker] = None,

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



    async def forward(self, request: Request, request_id: str) -> Response:

        """

        Forward *request* to the target service.



        Retries up to max_attempts on timeout or retryable status codes.

        Integrates with the circuit breaker if one was provided.

        """

        target_url = _build_target_url(request, self._route)

        body = await request.body()

        headers = _filter_headers(dict(request.headers))

        headers["X-Request-ID"] = request_id

        headers["X-Forwarded-For"] = request.client.host if request.client else "unknown"

        headers["X-Gateway"] = "FastAPI-Gateway/1.0"



        last_exc: Optional[Exception] = None



        async with httpx.AsyncClient(timeout=self._timeout) as client:

            for attempt in range(self._cfg.max_attempts):

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



                # Circuit breaker guard

                if self._cb:

                    try:

                        await self._cb.before_call()

                    except CircuitBreakerOpen as exc:

                        return Response(

                            content=f'{{"error": "Service unavailable (circuit open): {exc.service}"}}',

                            status_code=503,

                            media_type="application/json",

                        )



                try:

                    resp = await client.request(

                        method=request.method,

                        url=target_url,

                        headers=headers,

                        content=body,

                    )



                    if resp.status_code in self._cfg.retry_on_status and attempt < self._cfg.max_attempts - 1:

                        if self._cb:

                            await self._cb.on_failure()

                        last_exc = Exception(f"Retryable status {resp.status_code}")

                        continue



                    if self._cb:

                        if resp.status_code < 500:

                            await self._cb.on_success()

                        else:

                            await self._cb.on_failure()



                    response_headers = _filter_headers(dict(resp.headers))

                    response_headers["X-Request-ID"] = request_id



                    return Response(

                        content=resp.content,

                        status_code=resp.status_code,

                        headers=response_headers,

                        media_type=resp.headers.get("content-type"),

                    )



                except httpx.TimeoutException as exc:

                    logger.warning(

                        "Request timeout",

                        extra={

                            "attempt": attempt + 1,

                            "target": target_url,

                            "error": str(exc),

                            "request_id": request_id,

                        },

                    )

                    if self._cb:

                        await self._cb.on_failure()

                    last_exc = exc



                except httpx.RequestError as exc:

                    logger.warning(

                        "Request error",

                        extra={

                            "attempt": attempt + 1,

                            "target": target_url,

                            "error": str(exc),

                            "request_id": request_id,

                        },

                    )

                    if self._cb:

                        await self._cb.on_failure()

                    last_exc = exc



        # All attempts exhausted

        logger.error(

            "All retry attempts failed",

            extra={

                "target": target_url,

                "attempts": self._cfg.max_attempts,

                "error": str(last_exc),

                "request_id": request_id,

            },

        )

        return Response(

            content=f'{{"error": "Upstream unavailable after {self._cfg.max_attempts} attempts"}}',

            status_code=502,

            media_type="application/json",

        )