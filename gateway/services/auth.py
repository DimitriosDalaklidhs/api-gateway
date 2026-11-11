"""
JWT authentication.

create_token() mints a signed token, decode_token() verifies one, and
require_auth() is what the proxy router calls for a route with
auth_required: true.

The gateway authenticates and nothing more: it checks the signature, the
algorithm and the expiry. Roles travel in the token for downstream services to
act on; nothing in the gateway reads them.
"""

import logging
from datetime import UTC, datetime, timedelta

import jwt
from core.config import settings
from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("gateway.auth")

# auto_error=False: a missing header gives None instead of FastAPI's own error,
# so require_auth() chooses the status code and sets WWW-Authenticate (RFC 6750)
_bearer = HTTPBearer(auto_error=False)

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}


def create_token(subject: str, roles: list[str] | None = None) -> str:
    """Mint a signed JWT carrying sub, iat, exp and roles."""
    cfg = settings.jwt
    now = datetime.now(tz=UTC)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=cfg.expire_minutes),
        "roles": roles or [],
    }
    return jwt.encode(payload, cfg.secret_key, algorithm=cfg.algorithm)


def decode_token(token: str) -> dict:
    """Return the claims of a valid token; raise 401 for anything else."""
    cfg = settings.jwt
    try:
        return jwt.decode(
            token,
            cfg.secret_key,
            algorithms=[cfg.algorithm],  # the token's own "alg" header is never trusted
            # PyJWT checks exp only when the claim is there, so without this a
            # token signed with no exp would never expire
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers=_UNAUTHORIZED_HEADERS,
        ) from exc
    except jwt.InvalidTokenError as exc:
        # Why it failed (bad signature, wrong algorithm, missing claim) goes to
        # the log. Telling the caller would help them work out what we check.
        logger.info("Rejected token", extra={"reason": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers=_UNAUTHORIZED_HEADERS,
        ) from exc


async def require_auth(request: Request) -> dict:
    """Return the caller's verified claims, or raise 401."""
    creds: HTTPAuthorizationCredentials | None = await _bearer(request)
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers=_UNAUTHORIZED_HEADERS,
        )

    claims = decode_token(creds.credentials)
    logger.info("Authenticated request", extra={"sub": claims.get("sub"), "path": request.url.path})
    return claims
