"""
Request and response models for the gateway's own endpoints.
"""

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """Body of the errors the gateway's own code answers with (utils/errors.py)."""

    error: str
    retry_after: int | None = None  # seconds; set on 429


class TokenRequest(BaseModel):
    username: str
    password: str
    roles: list[str] = Field(default_factory=list)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
