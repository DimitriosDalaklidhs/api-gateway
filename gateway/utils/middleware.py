"""
Phase 2 — Request/Response logging middleware.

Logs: method, path, status, latency, client IP, request ID.
"""

import logging
import time
import uuid
from collections.abc import Callable

from core.logging import request_id_var
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from utils.errors import error_response

logger = logging.getLogger("gateway.middleware")

# Admin / health paths to skip verbose logging
_QUIET_PATHS = {"/admin/health", "/favicon.ico"}


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        client_ip = self._get_ip(request)
        path = request.url.path
        start = time.perf_counter()

        # The proxy router reads both, so logs, downstream and response agree
        request.state.request_id = request_id
        request.state.client_ip = client_ip

        # Every log line written while this request is in flight carries its ID
        token = request_id_var.set(request_id)
        try:
            return await self._dispatch(request, call_next, request_id, client_ip, path, start)
        finally:
            request_id_var.reset(token)

    async def _dispatch(
        self,
        request: Request,
        call_next: Callable,
        request_id: str,
        client_ip: str,
        path: str,
        start: float,
    ) -> Response:
        quiet = path in _QUIET_PATHS

        if not quiet:
            logger.info(
                "→ %s %s",
                request.method,
                path,
                extra={
                    "event": "request",
                    "request_id": request_id,
                    "method": request.method,
                    "path": path,
                    "client_ip": client_ip,
                    "user_agent": request.headers.get("user-agent", ""),
                },
            )

        try:
            response: Response = await call_next(request)
        except Exception:
            # Answered here, not re-raised. Starlette's last-resort handler sits
            # outside this middleware, so its 500 would reach the client without
            # the request ID that the log line for this failure carries.
            logger.error(
                "Unhandled exception",
                exc_info=True,
                extra={
                    "event": "error",
                    "request_id": request_id,
                    "method": request.method,
                    "path": path,
                    "client_ip": client_ip,
                },
            )
            response = error_response(500, "Internal gateway error")

        latency_ms = (time.perf_counter() - start) * 1000

        if not quiet:
            level = logging.WARNING if response.status_code >= 400 else logging.INFO
            logger.log(
                level,
                "← %s %s %d (%.1fms)",
                request.method,
                path,
                response.status_code,
                latency_ms,
                extra={
                    "event": "response",
                    "request_id": request_id,
                    "method": request.method,
                    "path": path,
                    "status_code": response.status_code,
                    "latency_ms": round(latency_ms, 2),
                    "client_ip": client_ip,
                },
            )

        response.headers["X-Request-ID"] = request_id
        return response

    @staticmethod
    def _get_ip(request: Request) -> str:
        # The TCP peer, never X-Forwarded-For: clients set that header themselves.
        # uvicorn would rewrite the peer from that header for trusted addresses
        # (127.0.0.1 by default, which is where kubectl port-forward traffic comes
        # from), so the image runs it with --no-proxy-headers.
        return request.client.host if request.client else "unknown"
