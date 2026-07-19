"""Fail-fast startup validator for BSS_PORTAL_TOKEN_PEPPER.

Called from the portal's lifespan BEFORE any auth flow can run. If the
pepper is unset, still the .env.example sentinel, or shorter than 32
chars, the portal refuses to start. Mirrors the bss-middleware
``validate_api_token_present`` pattern.
"""

from __future__ import annotations

import structlog

from .config import Settings

log = structlog.get_logger(__name__)

_SENTINEL = "changeme"
_MIN_LENGTH = 32


def validate_pepper_present() -> None:
    """Raise RuntimeError if BSS_PORTAL_TOKEN_PEPPER is unset / sentinel / short.

    Pepper rotation invalidates every in-flight login code, so it's
    deliberately a startup-only concern. Hot-reload is not supported.
    """
    pepper = Settings().BSS_PORTAL_TOKEN_PEPPER
    if not pepper:
        raise RuntimeError(
            "BSS_PORTAL_TOKEN_PEPPER is unset; set it in .env before starting "
            "the portal. Generate via: openssl rand -hex 32"
        )
    if pepper == _SENTINEL:
        raise RuntimeError(
            "BSS_PORTAL_TOKEN_PEPPER is still the .env.example sentinel "
            f"({_SENTINEL!r}); replace with a real value. "
            "Generate via: openssl rand -hex 32"
        )
    if len(pepper) < _MIN_LENGTH:
        raise RuntimeError(
            f"BSS_PORTAL_TOKEN_PEPPER too short ({len(pepper)} chars; need "
            f">={_MIN_LENGTH}). Generate via: openssl rand -hex 32"
        )
    log.info("portal_auth.pepper.validated", length=len(pepper))
