"""bss-middleware — shared ASGI middleware for BSS-CLI services.

v0.3 shipped the API token auth middleware: every BSS service requires
``X-BSS-API-Token: <BSS_API_TOKEN>`` on every request. Missing or
wrong token returns 401 before the endpoint runs.

v0.9 splits the perimeter into named tokens. The middleware now
validates against a ``TokenMap`` loaded from ``BSS_API_TOKEN`` plus
any ``BSS_<NAME>_API_TOKEN`` envs. A successful match attaches the
resolved ``service_identity`` to the ASGI scope so downstream
middleware can stamp it onto auth_context, audit rows, and OTel
spans. See ``phases/V0_9_0.md`` and ``DECISIONS.md`` 2026-04-26.

Future versions add other shared middleware (e.g. rate limiting in
post-Phase-12 work). They live in this package so consumers
``from bss_middleware import X`` works uniformly.
"""

from .api_token import (
    TokenMap,
    TokenMapInvalid,
    load_token_map_from_env,
    validate_token_map,
    validate_token_map_present,
)
from .config import Settings
from .startup import validate_api_token_present
from .token_auth import (
    AUTH_INVALID_TOKEN,
    AUTH_MISSING_TOKEN,
    EXEMPT_PATHS,
    SCOPE_SERVICE_IDENTITY,
    WEBHOOK_EXEMPT_PATHS,
    BSSApiTokenMiddleware,
)


def api_token() -> str:
    """Return the configured BSS_API_TOKEN. Raises if unset.

    Single source of truth for "where does the default token come from"
    so services and bss-clients caller construction don't drift.
    Reads via the same pydantic-settings path as the middleware.
    """
    token = Settings().BSS_API_TOKEN
    if not token:
        raise RuntimeError(
            "BSS_API_TOKEN not configured — call validate_token_map_present() "
            "in lifespan startup to fail fast on misconfig."
        )
    return token

# 64-char hex test token. Per-service conftest.py sets BSS_API_TOKEN
# to this value via monkeypatch and adds the header to its
# httpx.AsyncClient default-headers. Centralized so tests across
# the workspace use the same value.
TEST_TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

__all__ = [
    "AUTH_INVALID_TOKEN",
    "AUTH_MISSING_TOKEN",
    "BSSApiTokenMiddleware",
    "EXEMPT_PATHS",
    "SCOPE_SERVICE_IDENTITY",
    "TEST_TOKEN",
    "TokenMap",
    "TokenMapInvalid",
    "WEBHOOK_EXEMPT_PATHS",
    "api_token",
    "load_token_map_from_env",
    "validate_api_token_present",
    "validate_token_map",
    "validate_token_map_present",
]
