"""
JWT Authentication module for the API Gateway.

Handles two responsibilities:
  1. Token helpers — create_token() and decode_token() for minting and
     validating signed JWTs using settings from core.config.
  2. FastAPI dependencies — require_auth() and optional_auth() for injecting
     verified user claims into route handlers and middleware.

The /auth/token endpoint (defined in the router layer) calls create_token()
directly. Routes with auth_required=True in their RouteConfig use require_auth()
as a FastAPI dependency.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.config import settings

# Namespaced logger — output appears as "gateway.auth" in log aggregators
logger = logging.getLogger("gateway.auth")

# HTTPBearer extracts the token from the "Authorization: Bearer <token>" header.
# auto_error=False means it returns None instead of raising 403 when the header
# is absent, allowing require_auth / optional_auth to control the error response.
_bearer = HTTPBearer(auto_error=False)


# ══════════════════════════════════════════════════════════════════════════════
# Token Helpers
# ══════════════════════════════════════════════════════════════════════════════

def create_token(
    subject: str,
    roles: list[str] | None = None,
    extra: dict | None = None,
) -> str:
    """
    Mint a signed JWT for the given subject.

    Standard claims included:
      - sub  : the user identifier (email, username, UUID, etc.)
      - iat  : issued-at timestamp (UTC)
      - exp  : expiry timestamp, calculated from settings.jwt.expire_minutes
      - roles: list of role strings used for authorisation checks downstream

    Args:
        subject: Unique identifier for the token owner (stored in the 'sub' claim).
        roles:   Optional list of role strings (e.g. ["admin", "reader"]).
                 Defaults to an empty list if omitted.
        extra:   Optional dict of additional claims merged into the payload
                 (e.g. {"tenant_id": "acme"}). Keys must not clash with
                 standard claims (sub, iat, exp, roles).

    Returns:
        A signed JWT string ready to be sent in an Authorization header.
    """
    cfg = settings.jwt
    now = datetime.now(tz=timezone.utc)     # Always use UTC for JWT timestamps

    payload = {
        "sub":   subject,
        "iat":   now,
        "exp":   now + timedelta(minutes=cfg.expire_minutes),
        "roles": roles or [],
        **(extra or {}),    # Merge any caller-supplied custom claims last
    }

    return jwt.encode(payload, cfg.secret_key, algorithm=cfg.algorithm)


def decode_token(token: str) -> dict:
    """
    Decode and verify a JWT, returning its claims as a plain dict.

    Verification steps performed by PyJWT:
      - Signature validation against settings.jwt.secret_key
      - Expiry check (raises ExpiredSignatureError if 'exp' is in the past)
      - Algorithm check (only settings.jwt.algorithm is accepted)

    Args:
        token: Raw JWT string, typically extracted from an Authorization header.

    Returns:
        The verified payload dict (e.g. {"sub": "alice", "roles": [...], ...}).

    Raises:
        HTTPException 401: If the token is expired or otherwise invalid.
                           The WWW-Authenticate header is included per RFC 6750.
    """
    cfg = settings.jwt
    try:
        return jwt.decode(token, cfg.secret_key, algorithms=[cfg.algorithm])

    except jwt.ExpiredSignatureError:
        # Separate branch so callers can distinguish "expired" from "malformed"
        # in logs, even though both surface as 401 to the client
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )

    except jwt.InvalidTokenError as exc:
        # Catches all other PyJWT failures: bad signature, malformed structure,
        # wrong algorithm, missing required claims, etc.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ══════════════════════════════════════════════════════════════════════════════
# FastAPI Dependencies
# ══════════════════════════════════════════════════════════════════════════════

async def require_auth(request: Request) -> dict:
    """
    FastAPI dependency that enforces authentication on protected routes.

    Usage:
        @router.get("/protected", dependencies=[Depends(require_auth)])
        async def protected_route(claims: dict = Depends(require_auth)):
            user = claims["sub"]

    Flow:
        1. Extract the Bearer token from the Authorization header.
        2. Decode and verify the token via decode_token().
        3. Attach the verified claims to request.state.user so that
           middleware and other dependencies can read them without
           re-decoding the token.
        4. Log the authenticated request for audit purposes.

    Args:
        request: The incoming FastAPI Request object (injected automatically).

    Returns:
        The verified JWT claims dict (same shape as decode_token() output).

    Raises:
        HTTPException 401: If the Authorization header is absent or the token is invalid.
    """
    creds: Optional[HTTPAuthorizationCredentials] = await _bearer(request)

    if creds is None:
        # Header is missing entirely — return 401 rather than 403 per RFC 6750
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Decode and verify the raw token string; raises 401 on any failure
    claims = decode_token(creds.credentials)

    # Persist claims on request.state so middleware can read the user identity
    # without calling decode_token() a second time
    request.state.user = claims

    logger.info(
        "Authenticated request",
        extra={"sub": claims.get("sub"), "path": request.url.path},
    )

    return claims


async def optional_auth(request: Request) -> Optional[dict]:
    """
    FastAPI dependency for routes that support both authenticated and anonymous access.

    Behaves identically to require_auth() when a valid Bearer token is present,
    but returns None instead of raising 401 when the header is absent or the
    token is invalid. This allows route handlers to serve degraded or
    personalised responses without blocking unauthenticated users entirely.

    Usage:
        @router.get("/feed")
        async def feed(claims: Optional[dict] = Depends(optional_auth)):
            if claims:
                return personalised_feed(claims["sub"])
            return public_feed()

    Args:
        request: The incoming FastAPI Request object (injected automatically).

    Returns:
        The verified JWT claims dict if authentication succeeds, otherwise None.
    """
    creds: Optional[HTTPAuthorizationCredentials] = await _bearer(request)

    if creds is None:
        return None     # No Authorization header — anonymous request, allow through

    try:
        claims = decode_token(creds.credentials)
        request.state.user = claims     # Attach claims for consistent middleware access
        return claims
    except HTTPException:
        # Token present but invalid (expired, malformed, wrong key) —
        # treat as anonymous rather than returning 401, per optional semantics
        return None
