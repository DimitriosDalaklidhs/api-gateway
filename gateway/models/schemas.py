"""
Request and response models for the gateway's own endpoints.
"""

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
    request_id: str | None = None


class TokenRequest(BaseModel):
    username: str
    password: str
    roles: list[str] = Field(default_factory=list)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
