"""
Structured logging configuration.
Outputs JSON logs to stdout + rotating file.

Importing this module installs the handlers (main.py does that). Every record
logged while a request is in flight gets that request's ID, including records
from the services that never see the request object.
"""

import json
import logging
import logging.handlers
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from core.config import settings

# Set by LoggingMiddleware for the duration of each request. A ContextVar
# follows the request across awaits and into the tasks it spawns, so concurrent
# requests on the same event loop never see each other's ID.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


class RequestIdFilter(logging.Filter):
    """Stamp the current request ID onto records that don't carry one already."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            request_id = request_id_var.get()
            if request_id is not None:
                record.request_id = request_id
        return True


class JSONFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        base: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        # Merge any extra fields attached by callers
        for key, val in record.__dict__.items():
            if key not in {
                "args", "asctime", "created", "exc_info", "exc_text",
                "filename", "funcName", "id", "levelname", "levelno",
                "lineno", "module", "msecs", "message", "msg", "name",
                "pathname", "process", "processName", "relativeCreated",
                "stack_info", "taskName", "thread", "threadName",
            }:
                base[key] = val
        return json.dumps(base, default=str)


_configured = False


def setup_logging() -> logging.Logger:
    global _configured
    if _configured:  # adding the handlers twice would print every line twice
        return logging.getLogger("gateway")
    _configured = True

    log_cfg = settings.logging
    log_level = getattr(logging, log_cfg.level.upper(), logging.INFO)

    # Ensure log directory exists
    log_path = Path(log_cfg.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(log_level)

    formatter = JSONFormatter()
    # On the handlers, not a logger: handler filters also see records that
    # propagate up from child loggers like gateway.circuit_breaker.
    request_ids = RequestIdFilter()

    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(request_ids)
    root.addHandler(console)

    # Rotating file handler
    file_handler = logging.handlers.RotatingFileHandler(
        log_cfg.file,
        maxBytes=log_cfg.max_bytes,
        backupCount=log_cfg.backup_count,
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(request_ids)
    root.addHandler(file_handler)

    # Quieten noisy third-party loggers
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("gateway")


logger = setup_logging()


class RequestLogger:
    """Helper to log a request / response cycle."""

    @staticmethod
    def log_request(
        method: str,
        path: str,
        client_ip: str,
        request_id: str,
    ) -> float:
        start = time.perf_counter()
        logger.info(
            "→ request",
            extra={
                "request_id": request_id,
                "method": method,
                "path": path,
                "client_ip": client_ip,
            },
        )
        return start

    @staticmethod
    def log_response(
        method: str,
        path: str,
        status_code: int,
        latency_ms: float,
        request_id: str,
        target: str = "",
    ) -> None:
        level = logging.WARNING if status_code >= 400 else logging.INFO
        logger.log(
            level,
            "← response",
            extra={
                "request_id": request_id,
                "method": method,
                "path": path,
                "status_code": status_code,
                "latency_ms": round(latency_ms, 2),
                "target": target,
            },
        )
