"""
Core configuration management.
Loads from YAML file and environment variables.
"""

import os
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class RedisConfig(BaseModel):
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: Optional[str] = None

    @property
    def url(self) -> str:
        if self.password:
            return f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"


class RateLimitConfig(BaseModel):
    enabled: bool = True
    default_limit: int = 100
    window_seconds: int = 60
    ban_duration_seconds: int = 300


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = 5
    recovery_timeout_seconds: int = 30
    half_open_max_calls: int = 3


class RetryConfig(BaseModel):
    max_attempts: int = 3
    backoff_factor: float = 0.5
    retry_on_status: List[int] = [500, 502, 503, 504]


class ProxyConfig(BaseModel):
    timeout_seconds: int = 30
    connect_timeout_seconds: int = 5


class RouteConfig(BaseModel):
    path: str
    target: str
    rate_limit: int = 100
    strip_prefix: bool = False
    methods: List[str] = ["GET", "POST", "PUT", "DELETE", "PATCH"]
    auth_required: bool = False


class JWTConfig(BaseModel):
    secret_key: str = "change-me"
    algorithm: str = "HS256"
    expire_minutes: int = 60


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "logs/gateway.log"
    max_bytes: int = 10_485_760
    backup_count: int = 5


class CachingConfig(BaseModel):
    enabled: bool = True
    default_ttl_seconds: int = 60
    cacheable_methods: List[str] = ["GET"]
    cache_prefix: str = "cache:"


class GatewayConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    title: str = "API Gateway"
    version: str = "1.0.0"


class Settings(BaseModel):
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    rate_limiting: RateLimitConfig = Field(default_factory=RateLimitConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    routes: List[RouteConfig] = Field(default_factory=list)
    jwt: JWTConfig = Field(default_factory=JWTConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    caching: CachingConfig = Field(default_factory=CachingConfig)

    @property
    def route_map(self) -> Dict[str, RouteConfig]:
        """Fast lookup: path prefix → RouteConfig."""
        return {r.path: r for r in self.routes}


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def load_settings(config_path: str = "config.yaml") -> Settings:
    base = Path(__file__).parent.parent
    data = _load_yaml(base / config_path)

    # Environment variable overrides
    if redis_host := os.getenv("REDIS_HOST"):
        data.setdefault("redis", {})["host"] = redis_host
    if redis_port := os.getenv("REDIS_PORT"):
        data.setdefault("redis", {})["port"] = int(redis_port)
    if jwt_secret := os.getenv("JWT_SECRET_KEY"):
        data.setdefault("jwt", {})["secret_key"] = jwt_secret

    return Settings(**data)


# Singleton
settings = load_settings()