"""
Shared Pydantic models used across the gateway.
"""

from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
    request_id: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    redis: str
    version: str


class RateLimitStats(BaseModel):
    ip: str
    count: int
    limit: int
    remaining: int
    reset_in_seconds: int
    banned: bool


class CircuitBreakerStatus(BaseModel):
    service: str
    state: str
    failures: int
    threshold: int
    opened_at: Optional[float] = None


class TokenRequest(BaseModel):
    username: str
    password: str
    roles: list[str] = Field(default_factory=list)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class MetricsSummary(BaseModel):
    uptime_seconds: float
    total_requests: int
    routes: list[Dict[str, Any]]
    circuit_breakers: list[Dict[str, Any]]