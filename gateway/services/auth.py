"""
Bonus — JWT Authentication.

Validates Bearer tokens on routes with auth_required=true.
Also exposes a /auth/token endpoint for obtaining tokens.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.config import settings

logger = logging.getLogger("gateway.auth")

_bearer = HTTPBearer(auto_error=False)


# ─────────────────────────────────────────────────────────────────────────────
# Token helpers
# ─────────────────────────────────────────────────────────────────────────────

def create_token(subject: str, roles: list[str] | None = None, extra: dict | None = None) -> str:
    cfg = settings.jwt
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=cfg.expire_minutes),
        "roles": roles or [],
        **(extra or {}),
    }
    return jwt.encode(payload, cfg.secret_key, algorithm=cfg.algorithm)


def decode_token(token: str) -> dict:
    cfg = settings.jwt
    try:
        return jwt.decode(token, cfg.secret_key, algorithms=[cfg.algorithm])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI dependency
# ─────────────────────────────────────────────────────────────────────────────

async def require_auth(request: Request) -> dict:
    """
    FastAPI dependency that validates the Bearer token.
    Attach decoded claims to request.state for downstream use.
    """
    creds: Optional[HTTPAuthorizationCredentials] = await _bearer(request)
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = decode_token(creds.credentials)
    request.state.user = claims
    logger.info(
        "Authenticated request",
        extra={"sub": claims.get("sub"), "path": request.url.path},
    )
    return claims


async def optional_auth(request: Request) -> Optional[dict]:
    """Like require_auth but returns None for unauthenticated requests."""
    creds: Optional[HTTPAuthorizationCredentials] = await _bearer(request)
    if creds is None:
        return None
    try:
        claims = decode_token(creds.credentials)
        request.state.user = claims
        return claims
    except HTTPException:
        return None