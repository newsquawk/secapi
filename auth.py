"""
Authentication wiring for secapi (JWKS verification via newsquawk-auth).

Policy:
  * Every data endpoint requires a JWKS-verified bearer token (``require_authenticated``).
  * The sync / Content Hub pull endpoints (`/changes`, `/changes/head`)
    additionally require a Keycloak **service account** holding the configured
    realm role (`AUTH_SYNC_REALM_ROLE`) — applied via ``sync_dependencies``.
  * `/stream` and `/health` stay public (wired in main.py / sync.py).

This service does not itself call other services, so it needs no service-account
of its own — only the receiver-side verification here.
"""

from fastapi import Depends

from newsquawk_auth import AuthService, AuthDependencies

from config import APP_ENV, logger
from auth_settings import auth_settings

# Production mandates a real JWKS endpoint: AuthService raises if AUTH_JWKS_URL is
# unset and stub_mode is False, so a misconfigured prod deploy fails closed. In
# non-production, fall back to stub mode when no JWKS URL is set so local dev and
# tests can run without a Keycloak (stub still verifies HS256 signatures/expiry).
_stub_mode = auth_settings.stub_mode or (
    APP_ENV != "production" and not auth_settings.jwks_url
)

auth_service = AuthService(
    jwks_url=auth_settings.jwks_url,
    audience=auth_settings.audience,
    jwks_cache_lifespan=auth_settings.jwks_cache_lifespan,
    jwks_timeout=auth_settings.jwks_timeout,
    stub_mode=_stub_mode,
    stub_secret=auth_settings.stub_secret,
)
auth_deps = AuthDependencies(auth_service)

if _stub_mode:
    logger.warning("secapi auth running in STUB MODE — not for production.")

# --- Reusable dependencies -------------------------------------------------
# Minimum: any principal presenting a JWKS-verified token.
require_authenticated = Depends(auth_deps.get_current_user())

# Sync endpoints: a Keycloak service account that ALSO holds the configured realm
# role. Both are asserted together (per the library's service-account guidance).
sync_dependencies = [
    Depends(auth_deps.require_service_account()),
    Depends(auth_deps.has_realm_role(auth_settings.sync_realm_role)),
]
