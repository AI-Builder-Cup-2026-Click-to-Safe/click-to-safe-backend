"""Shared-token authentication for the public Cloud Run surface.

Agent Runtime authenticates callers with Google IAM, so nothing here runs there.
Cloud Run serves the same image to the open internet, where a caller presents a
fixed token instead. The token is injected as API_TOKEN at deploy time.
"""

import hmac
import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Liveness must answer before a token exists, otherwise Cloud Run cannot tell a
# misconfigured revision from a crashed one and reports neither.
PUBLIC_PATHS = frozenset({"/healthz"})
SCHEME = "bearer"


def expected_token() -> str:
    return os.getenv("API_TOKEN", "")


def presented_token(header: str) -> str:
    """Accept `Bearer <token>`, and a bare token for callers that omit the scheme."""
    scheme, _, rest = header.partition(" ")
    if rest and scheme.lower() == SCHEME:
        return rest.strip()
    return header.strip() if not _ else ""


def token_matches(header: str, secret: str) -> bool:
    presented = presented_token(header)
    if not presented:
        return False
    # Constant time: a plain == leaks the shared secret one byte at a time
    # through response timing, and this endpoint is open to the internet.
    return hmac.compare_digest(presented, secret)


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Reject every request that does not carry the shared token.

    Applied as middleware rather than a per-route dependency so that a route
    added later is authenticated by default instead of by remembering to.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        secret = expected_token()
        if not secret:
            # Refuse rather than fall open: an unset variable is a deploy fault,
            # and serving it unauthenticated would publish the API to anyone.
            logger.error("API_TOKEN is not configured; rejecting request")
            return JSONResponse({"detail": "Server is not configured for authentication"}, 503)
        if not token_matches(request.headers.get("Authorization", ""), secret):
            return JSONResponse(
                {"detail": "Missing or invalid Authorization header"},
                401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


def install(app) -> None:
    """Guard the app only where a shared token is the authentication model.

    Agent Runtime fronts the same image with IAM and injects no API_TOKEN, so
    enabling the middleware there would reject every authorised caller.
    """
    if os.getenv("PUBLIC_API") == "true":
        app.add_middleware(TokenAuthMiddleware)
