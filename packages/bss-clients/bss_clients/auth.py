"""AuthProvider protocol and built-in implementations.

Every bss-clients client takes an AuthProvider at construction and calls it
on every outgoing request.

- ``NoAuthProvider`` — v0.1 default, no headers added. Used in tests.
- ``TokenAuthProvider`` — v0.3+ default everywhere in production wiring;
  injects ``X-BSS-API-Token: <BSS_API_TOKEN>`` so the receiving service's
  ``BSSApiTokenMiddleware`` accepts the request.
- ``NamedTokenAuthProvider`` — v0.9+ for external-facing surfaces (the
  self-serve portal today; partner clients later). Reads its token from
  a named env var (``BSS_PORTAL_SELF_SERVE_API_TOKEN`` etc.) and carries an
  informational identity label for log fields. The actual
  ``service_identity`` resolved on the receiving side comes from token
  validation, not the label — the label exists only so the *outbound*
  side can log "which provider sent this call" without a separate trip.

Phase 12 will add ``OAuth2ClientCredentialsProvider`` here. No hardcoded
auth headers anywhere else in the codebase.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

import structlog

log = structlog.get_logger(__name__)


@runtime_checkable
class AuthProvider(Protocol):
    """Pluggable auth for service-to-service calls."""

    async def get_headers(self) -> dict[str, str]:
        """Return auth headers to inject into every outgoing request."""
        ...


class NoAuthProvider:
    """No-op provider used by tests and pre-v0.3 code paths."""

    async def get_headers(self) -> dict[str, str]:
        return {}


class TokenAuthProvider:
    """Injects ``X-BSS-API-Token`` on every outbound request.

    Construct with the shared ``BSS_API_TOKEN`` value (typically read from
    env in the caller's config layer). Empty token raises immediately —
    a valid client cannot exist with no token, by construction.
    """

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("TokenAuthProvider requires a non-empty token")
        self._headers = {"X-BSS-API-Token": token}

    async def get_headers(self) -> dict[str, str]:
        return dict(self._headers)


class BearerAuthProvider:
    """v1.1 — ``Authorization: Bearer <token>`` for external services.

    Used by ``LoyaltyClient`` to authenticate against samurai-bot/loyalty-cli,
    whose HTTP surface expects a bearer token (not BSS's ``X-BSS-API-Token``).
    Construct with the value of ``BSS_LOYALTY_API_TOKEN`` (read from env in
    the caller's config layer; the token never leaves the BSS process that
    holds it). Empty token raises immediately — same fail-fast posture as
    ``TokenAuthProvider``.
    """

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("BearerAuthProvider requires a non-empty token")
        self._headers = {"Authorization": f"Bearer {token}"}

    async def get_headers(self) -> dict[str, str]:
        return dict(self._headers)


class NamedTokenAuthProvider:
    """v0.9 — outbound auth provider for an external-facing surface.

    Loads its token from ``env_var`` (e.g. ``BSS_PORTAL_SELF_SERVE_API_TOKEN``)
    once at construction and caches it. ``identity`` is an
    informational label used in log fields ("which provider sent this
    call") — it is **not** stamped onto the outbound request as a
    header. The receiving service derives the authoritative
    ``service_identity`` from token validation against its own
    TokenMap, never from a caller-asserted header.

    Backwards compat: if ``env_var`` is unset and ``fallback_env_var``
    is provided + populated, use the fallback's value and log a
    one-time warning. The receiving service will resolve the
    fallback token to whatever identity it maps to (typically
    ``"default"`` when ``fallback_env_var=BSS_API_TOKEN``). This lets
    a portal continue to function during a staged rollout where
    ``BSS_PORTAL_SELF_SERVE_API_TOKEN`` hasn't been provisioned yet.

    If neither env is populated, raises at construction so the
    portal's lifespan fails fast (matches the v0.3 fail-fast pattern).
    """

    def __init__(
        self,
        identity: str,
        env_var: str,
        *,
        fallback_env_var: str | None = None,
    ) -> None:
        if not identity:
            raise ValueError("NamedTokenAuthProvider requires a non-empty identity")
        if not env_var:
            raise ValueError("NamedTokenAuthProvider requires an env_var name")

        self._identity = identity
        self._env_var = env_var

        primary = os.environ.get(env_var, "")
        if primary:
            self._token = primary
            self._source_env = env_var
        elif fallback_env_var and os.environ.get(fallback_env_var, ""):
            self._token = os.environ[fallback_env_var]
            self._source_env = fallback_env_var
            log.warning(
                "auth.named_token.fallback",
                identity=identity,
                primary_env=env_var,
                fallback_env=fallback_env_var,
                note=(
                    "primary env unset; falling back to default token. "
                    "Receiving services will see service_identity=default "
                    "instead of the named identity until the primary env "
                    "is configured."
                ),
            )
        else:
            raise RuntimeError(
                f"NamedTokenAuthProvider({identity!r}): {env_var} is unset"
                + (
                    f" and fallback {fallback_env_var} is also unset"
                    if fallback_env_var else ""
                )
                + ". Generate via: openssl rand -hex 32"
            )
        self._headers = {"X-BSS-API-Token": self._token}

    @property
    def identity(self) -> str:
        """The informational identity label. Used in caller-side logs only."""
        return self._identity

    @property
    def source_env(self) -> str:
        """The env var the token was loaded from. ``env_var`` or fallback."""
        return self._source_env

    async def get_headers(self) -> dict[str, str]:
        return dict(self._headers)
