import threading
import time
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends, Header, HTTPException, status
from jose import JWTError, jwt
from supabase import Client, create_client

from app.core.config import settings
from app.db.supabase import get_supabase_admin


@dataclass
class Auth:
    user_id: UUID
    db: Client


# JWKS cache for asymmetric tokens (ES256 / RS256). Supabase rotates keys
# rarely, so caching the public set for an hour is plenty.
_JWKS_CACHE: dict | None = None
_JWKS_FETCHED_AT: float = 0.0
_JWKS_TTL_SECONDS = 3600.0
_JWKS_LOCK = threading.Lock()


def _jwks_url() -> str:
    return f"{settings.SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json"


def _fetch_jwks(force: bool = False) -> dict:
    global _JWKS_CACHE, _JWKS_FETCHED_AT
    with _JWKS_LOCK:
        now = time.time()
        if (
            not force
            and _JWKS_CACHE is not None
            and (now - _JWKS_FETCHED_AT) < _JWKS_TTL_SECONDS
        ):
            return _JWKS_CACHE
        if not settings.SUPABASE_URL:
            raise JWTError("SUPABASE_URL not configured for JWKS fetch")
        try:
            resp = httpx.get(_jwks_url(), timeout=5.0)
            resp.raise_for_status()
        except httpx.RequestError as exc:
            raise JWTError(f"failed to fetch JWKS: {exc}") from exc
        _JWKS_CACHE = resp.json()
        _JWKS_FETCHED_AT = now
        return _JWKS_CACHE


def _find_jwk(kid: str) -> dict:
    jwks = _fetch_jwks()
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if key is not None:
        return key
    # one retry with forced refresh in case of key rotation
    jwks = _fetch_jwks(force=True)
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if key is None:
        raise JWTError(f"no JWKS key matching kid={kid}")
    return key


def decode_supabase_jwt(token: str) -> dict:
    """Verify a Supabase JWT. Supports both HS256 (legacy SUPABASE_JWT_SECRET)
    and asymmetric ES256 / RS256 (newer projects, via JWKS endpoint
    `<SUPABASE_URL>/auth/v1/.well-known/jwks.json`). Raises JWTError."""
    header = jwt.get_unverified_header(token)
    alg = header.get("alg")

    if alg == "HS256":
        if not settings.SUPABASE_JWT_SECRET:
            raise JWTError("SUPABASE_JWT_SECRET not configured")
        return jwt.decode(
            token,
            settings.SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )

    if alg in ("ES256", "RS256"):
        kid = header.get("kid")
        if not kid:
            raise JWTError("token header missing kid")
        key = _find_jwk(kid)
        return jwt.decode(
            token,
            key,
            algorithms=[alg],
            options={"verify_aud": False},
        )

    raise JWTError(f"unsupported alg: {alg}")


def supabase_client_for_token(token: str) -> Client:
    """Build a per-request Supabase client whose PostgREST calls carry the
    user's JWT, so RLS evaluates against the authenticated identity."""
    if not settings.SUPABASE_URL or not settings.SUPABASE_ANON_KEY:
        raise RuntimeError("Supabase credentials not configured")
    client = create_client(settings.SUPABASE_URL, settings.SUPABASE_ANON_KEY)
    client.postgrest.auth(token)
    return client


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    return authorization.removeprefix("Bearer ")


def get_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> Auth:
    token = _extract_bearer(authorization)
    try:
        payload = decode_supabase_jwt(token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        ) from exc

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing sub claim",
        )
    return Auth(user_id=UUID(sub), db=supabase_client_for_token(token))


def get_admin_db() -> Client:
    return get_supabase_admin()


AuthDep = Annotated[Auth, Depends(get_auth)]
AdminDB = Annotated[Client, Depends(get_admin_db)]
