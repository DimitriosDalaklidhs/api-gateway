"""
Phase 2 — Request/Response logging middleware.

Logs: method, path, status, latency, client IP, request ID.
"""

import logging
import time
import uuid
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger("gateway.middleware")

# Admin / health paths to skip verbose logging
_QUIET_PATHS = {"/admin/health", "/favicon.ico"}


class LoggingMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        client_ip = self._get_ip(request)
        path = request.url.path
        start = time.perf_counter()

        # Attach request_id for use in downstream handlers
        request.state.request_id = request_id
        request.state.client_ip = client_ip
        request.state.start_time = start

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
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            logger.error(
                "Unhandled exception",
                exc_info=True,
                extra={
                    "event": "error",
                    "request_id": request_id,
                    "method": request.method,
                    "path": path,
                    "latency_ms": round(latency_ms, 2),
                    "client_ip": client_ip,
                },
            )
            raise

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
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "unknown"