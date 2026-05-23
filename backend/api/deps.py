"""Shared FastAPI dependencies: admin auth, IP extraction, hashing."""

from __future__ import annotations

from fastapi import Header, HTTPException, Request, status

from config import get_settings

from superforecaster.db import hash_ip


async def require_admin(authorization: str | None = Header(default=None)) -> None:
    """Require `Authorization: Bearer <ADMIN_API_KEY>` for admin routes."""
    expected = get_settings().admin_api_key
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="server misconfigured: ADMIN_API_KEY not set",
        )
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="missing bearer token")
    token = authorization[len("Bearer "):]
    if token != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid bearer token")


def get_client_ip(request: Request) -> str:
    """Extract the client IP, respecting X-Forwarded-For and X-Real-IP."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    if request.client is None:
        return "unknown"
    return request.client.host


def get_client_ip_hash(request: Request) -> str:
    """SHA-256 of the client IP — what gets stored / queried."""
    return hash_ip(get_client_ip(request))
