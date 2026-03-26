"""
Structured logging configuration.
Outputs JSON logs to stdout + rotating file.
"""

import json
import logging
import logging.handlers
import time
from pathlib import Path
from typing import Any, Dict

from core.config import settings


class JSONFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        base: Dict[str, Any] = {
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
                "stack_info", "thread", "threadName",
            }:
                base[key] = val
        return json.dumps(base, default=str)


def setup_logging() -> logging.Logger:
    log_cfg = settings.logging
    log_level = getattr(logging, log_cfg.level.upper(), logging.INFO)

    # Ensure log directory exists
    log_path = Path(log_cfg.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(log_level)

    formatter = JSONFormatter()

    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    # Rotating file handler
    file_handler = logging.handlers.RotatingFileHandler(
        log_cfg.file,
        maxBytes=log_cfg.max_bytes,
        backupCount=log_cfg.backup_count,
    )
    file_handler.setFormatter(formatter)
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