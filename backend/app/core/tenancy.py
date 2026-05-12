"""Tenant resolution for every FastAPI request.

    Enterprise isolation layer - mandatory for SaaS scalability.

This module is the SINGLE source of truth for `org_id` inside the request
lifecycle.  Every route that reads or writes tenant data MUST depend on
``resolve_tenant_context`` and forward the resolved ``TenantContext.org_id``
into every Supabase call as ``.eq("org_id", current_org_id)``.

Resolution order (first hit wins):

    1. Authorization: Bearer <jwt> -> verified Supabase HS256 JWT.
       If the header IS present, the JWT MUST verify; we do NOT fall
       through to the headers below on a bad signature.  This prevents
       "send a forged JWT + a real X-Org-Id" confusion attacks.
    2. ``X-Org-Id`` header -- direct UUID, used by trusted server-to-server
       callers (lead webhooks, internal cron, integration tests).
    3. ``X-Org-Slug`` header -- human-readable slug looked up against
       ``public.organizations`` (LRU cached).

If none of the above resolve to a real org we return 401.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any

import jwt
from fastapi import Depends, Header, HTTPException, status
from jwt import PyJWKClient

from app.core.config import Settings
from app.db.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenantContext:
    """Resolved tenant identity for the current request.

        Enterprise isolation layer - mandatory for SaaS scalability.

    ``org_id`` is the only field every downstream call truly needs;
    ``slug`` and ``subscription_tier`` are convenience metadata for
    feature-gating (e.g. trial vs pro rate limits).  ``user_id`` is set
    only when the caller authenticated via JWT.
    """

    org_id: str
    slug: str | None = None
    subscription_tier: str | None = None
    user_id: str | None = None
    auth_method: str = "header"  # "jwt" | "header" | "slug"


def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


@lru_cache(maxsize=1)
def _settings_cached() -> Settings:
    return Settings.load()


@lru_cache(maxsize=1)
def _jwks_client_cached() -> PyJWKClient | None:
    """Cached JWKS client for Supabase's asymmetric (ES256/RS256) keys.

        Enterprise isolation layer - mandatory for SaaS scalability.
    Newer Supabase projects sign auth JWTs with rotating asymmetric keys
    (ES256 P-256 by default).  We fetch the public keys from the
    project's JWKS endpoint once and let PyJWKClient handle in-memory
    caching + on-demand refresh when a JWT carries an unknown ``kid``.
    """
    settings = _settings_cached()
    if not settings.supabase_url:
        return None
    jwks_url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
    try:
        # cache_keys=True keeps fetched keys in process memory; lifespan is
        # the soft TTL (seconds) before PyJWKClient revalidates on miss.
        return PyJWKClient(jwks_url, cache_keys=True, lifespan=300)
    except Exception as exc:
        logger.warning("Could not initialise JWKS client at %s: %s", jwks_url, exc)
        return None


def _verify_with_jwks(token: str, settings: Settings) -> dict[str, Any] | None:
    """Verify a JWT using Supabase's published JWKS public keys.

    Returns claims on success, None when this verifier shouldn't run for
    this token (e.g. it's HS256, or the JWKS is unreachable).  Raises
    HTTPException only when the token is asymmetric AND verification
    fails for a meaningful reason (expiry / audience / bad signature).
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return None

    alg = header.get("alg") or ""
    if alg.upper() in {"HS256", "HS384", "HS512"}:
        # Symmetric token; let the HS256 path handle it.
        return None

    client = _jwks_client_cached()
    if client is None:
        return None

    try:
        signing_key = client.get_signing_key_from_jwt(token)
    except Exception as exc:
        logger.info("JWKS lookup failed for kid=%s: %s", header.get("kid"), exc)
        return None

    try:
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=[alg],
            audience=settings.supabase_jwt_audience or None,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": bool(settings.supabase_jwt_audience),
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization JWT has expired.",
        ) from exc
    except jwt.InvalidAudienceError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization JWT audience is invalid.",
        ) from exc
    except jwt.InvalidSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization JWT signature is invalid.",
        ) from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authorization JWT is invalid ({type(exc).__name__}).",
        ) from exc


def _verify_with_hs256(token: str, settings: Settings) -> dict[str, Any] | None:
    """Verify a JWT using the legacy HS256 shared secret.

    Used for projects still on Supabase's "Legacy JWT Secret" mode and
    for our own internally-minted service-to-service tokens.  Returns
    None when the secret isn't configured or the token isn't HS256.
    """
    if not settings.supabase_jwt_secret:
        return None

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return None

    alg = (header.get("alg") or "").upper()
    if alg and alg != "HS256":
        # Asymmetric token; the JWKS path is the right one.
        return None

    try:
        return jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience=settings.supabase_jwt_audience or None,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": bool(settings.supabase_jwt_audience),
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization JWT has expired.",
        ) from exc
    except jwt.InvalidAudienceError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization JWT audience is invalid.",
        ) from exc
    except jwt.InvalidSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization JWT signature is invalid.",
        ) from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authorization JWT is invalid ({type(exc).__name__}).",
        ) from exc


def _verify_supabase_jwt(token: str) -> dict[str, Any] | None:
    """Verify a Supabase-issued JWT and return its claims.

        Enterprise isolation layer - mandatory for SaaS scalability.

    Resolution order (the FIRST verifier to recognise the algorithm wins):
      1. Asymmetric (ES256 / RS256) via the project's JWKS endpoint.
         This is the production path for users authenticated through
         Supabase Auth on modern projects.
      2. Symmetric HS256 via the project's "Legacy JWT Secret".  Used by
         service-to-service callers and projects that haven't migrated
         to asymmetric keys.

    Returns None when the JWT path is fully disabled (no JWKS reachable
    AND no HS256 secret set) so callers can fall through to header
    auth.  Raises HTTPException(401) if a token is provided but verifies
    against neither path -- we never silently fall through to X-Org-Id
    on a bad JWT (forged-JWT-plus-real-X-Org-Id confusion attack).
    """
    settings = _settings_cached()

    # 1. Asymmetric path (ES256/RS256 via JWKS).
    claims = _verify_with_jwks(token, settings)
    if claims is not None:
        return claims

    # 2. Legacy HS256 path.
    claims = _verify_with_hs256(token, settings)
    if claims is not None:
        return claims

    # If we got here AND at least one verifier was configured, the token
    # carried an unsupported algorithm or an unknown signing key.
    if _jwks_client_cached() is not None or settings.supabase_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Authorization JWT is not verifiable: algorithm is unsupported or its "
                "signing key is unknown to this server (JWKS / HS256)."
            ),
        )

    # JWT path entirely disabled by config; caller must use X-Org-Id / X-Org-Slug.
    return None


def _extract_org_from_jwt(authorization: str | None) -> tuple[str, str | None] | None:
    """Returns (org_id, user_id) on success, or None when no usable JWT was sent."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    claims = _verify_supabase_jwt(token)
    if claims is None:
        # JWT path disabled by config -- ignore the header and let the
        # caller fall through to X-Org-Id / X-Org-Slug.
        return None

    # Accept a top-level claim or one nested under app_metadata
    # (Supabase's default location for custom claims).
    candidate = claims.get("org_id") or (claims.get("app_metadata") or {}).get("org_id")
    if not isinstance(candidate, str) or not _is_valid_uuid(candidate):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization JWT is missing a valid org_id claim.",
        )

    user_id_claim = claims.get("sub") if isinstance(claims.get("sub"), str) else None
    return (candidate, user_id_claim)


@lru_cache(maxsize=1024)
def _lookup_org_by_slug(slug: str) -> tuple[str, str, str] | None:
    """Cached slug -> (org_id, slug, subscription_tier).

    A 1024-entry LRU is more than enough for 200+ orgs and sidesteps a
    Supabase round-trip on every single request from a slug-using SDK.
    """
    supabase = get_supabase_client()
    response = (
        supabase.table("organizations")
        .select("id,slug,subscription_tier")
        .eq("slug", slug)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    if not rows:
        return None
    row = rows[0]
    return (
        str(row["id"]),
        str(row.get("slug") or slug),
        str(row.get("subscription_tier") or "trial"),
    )


async def resolve_tenant_context(
    authorization: Annotated[str | None, Header()] = None,
    x_org_id: Annotated[str | None, Header(alias="X-Org-Id")] = None,
    x_org_slug: Annotated[str | None, Header(alias="X-Org-Slug")] = None,
) -> TenantContext:
    """FastAPI dependency that returns the tenant context for the request.

    Raises 401 if no usable identity is provided OR if a provided JWT is
    invalid.  Raises 400 only when the caller sent something but it was
    malformed (UUID/slug shape).
    """
    # 1. Verified JWT wins.  An INVALID JWT is a hard 401 (raised inside
    # _extract_org_from_jwt); we never silently fall through.
    jwt_result = _extract_org_from_jwt(authorization)
    if jwt_result:
        org_id, user_id = jwt_result
        return TenantContext(org_id=org_id, user_id=user_id, auth_method="jwt")

    # 2. Direct UUID header (server-to-server / webhook callers).
    if x_org_id is not None:
        if not _is_valid_uuid(x_org_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Org-Id must be a valid UUID.",
            )
        return TenantContext(org_id=x_org_id, auth_method="header")

    # 3. Slug header (developer-friendly identifier).
    if x_org_slug:
        slug = x_org_slug.strip().lower()
        if not slug:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Org-Slug must not be empty.",
            )
        resolved = _lookup_org_by_slug(slug)
        if not resolved:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Organization slug '{slug}' not found.",
            )
        org_id_str, slug_str, tier_str = resolved
        return TenantContext(
            org_id=org_id_str,
            slug=slug_str,
            subscription_tier=tier_str,
            auth_method="slug",
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "Tenant context missing. Send Authorization: Bearer <jwt> with an org_id claim, "
            "X-Org-Id (UUID), or X-Org-Slug to identify the tenant."
        ),
    )


TenantDep = Annotated[TenantContext, Depends(resolve_tenant_context)]


def reset_tenant_cache() -> None:
    """Clear in-process caches. Useful for tests and after org renames."""
    _lookup_org_by_slug.cache_clear()
    _settings_cached.cache_clear()
    _jwks_client_cached.cache_clear()
