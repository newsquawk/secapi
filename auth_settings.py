"""
Pydantic settings for authentication (newsquawk-auth / Keycloak JWKS).

Read from ``AUTH_``-prefixed environment variables, e.g. ``AUTH_JWKS_URL``,
``AUTH_SYNC_REALM_ROLE``. Production must set ``AUTH_JWKS_URL``; in non-production
the app falls back to stub mode when it is unset (see ``auth.py``) so local dev
and tests can run without a Keycloak.
"""

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTH_", extra="ignore")

    # --- JWKS token verification (the minimum every protected endpoint enforces) ---
    # URL of the Keycloak JWKS endpoint (.../protocol/openid-connect/certs).
    jwks_url: Optional[str] = None
    # Expected `aud` claim. Optional: when unset, audience is not checked
    # (signature + expiry are still verified).
    audience: Optional[str] = None
    jwks_cache_lifespan: int = 300  # seconds to cache fetched keys
    jwks_timeout: float = 5.0  # seconds for the JWKS HTTP fetch

    # --- Authorization for the sync (Content Hub pull) endpoints ---
    # Realm role a Keycloak service account must hold to read /changes*.
    sync_realm_role: str = "sec-sync"

    # --- Local dev / tests ONLY — never enable in production ---
    stub_mode: bool = False
    stub_secret: Optional[str] = None


auth_settings = AuthSettings()
