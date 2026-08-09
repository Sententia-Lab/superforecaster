"""Shared FastAPI dependencies: admin auth, local-mode detection."""

from __future__ import annotations

from fastapi import Header, HTTPException, Request, status

from config import get_settings


_PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "x-forwarded-host", "forwarded")

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def is_local_mode(request: Request) -> bool:
    """True when this request is a single user on their own machine, unauthenticated.

    `ADMIN_API_KEY` protects a deployment from the internet. On a laptop, where the only
    thing that can reach the port is the person who started the process, it protects
    nothing and costs the whole first-run experience: you export an API key, start the
    server, type a question, and the button says "Admin token not set."

    So an unset key is read as "not deployed" rather than "misconfigured" — but only for a
    request that came from loopback and carries no proxy header. Anything forwarded might
    have had its origin rewritten by something upstream, and a reverse proxy in front of
    this is exactly the shape of a real deployment, so those still need the key.
    """
    if get_settings().admin_api_key:
        return False
    if any(h in request.headers for h in _PROXY_HEADERS):
        return False
    return request.client is not None and request.client.host in _LOOPBACK


async def require_admin(
    request: Request, authorization: str | None = Header(default=None)
) -> None:
    """Require `Authorization: Bearer <ADMIN_API_KEY>` for admin routes.

    Skipped entirely in local mode — see `is_local_mode`.
    """
    expected = get_settings().admin_api_key
    if not expected:
        if is_local_mode(request):
            return
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ADMIN_API_KEY is not set, and this request did not come from "
            "localhost. Set ADMIN_API_KEY to serve this anywhere other than your own "
            "machine.",
        )
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="missing bearer token"
        )
    token = authorization[len("Bearer ") :]
    if token != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="invalid bearer token"
        )
