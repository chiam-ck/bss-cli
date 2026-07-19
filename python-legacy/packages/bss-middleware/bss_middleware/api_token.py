"""TokenMap — named-token loader + validator (v0.9+).

v0.3 introduced a single ``BSS_API_TOKEN`` shared by every internal
caller. v0.9 splits the perimeter: each external-facing surface
(self-serve portal, future partner integrations) gets its own named
token. The middleware validates an incoming ``X-BSS-API-Token``
against a map of {hashed_token: service_identity} loaded once at
startup. A successful match attaches ``service_identity`` to the
ASGI scope so request-context middleware can stamp it onto
``auth_context``, audit rows, log records, and OTel spans.

Design notes:

- The map is **hashed**. We HMAC-SHA-256 each env-loaded token with a
  fixed salt at startup; the raw value is not retained on the map
  itself. This lets ops log the loaded map at debug level for diagnosis
  without leaking secrets, and aligns with how peppered secrets are
  typically handled. The salt is a constant in source — its purpose
  is one-wayness across processes that share the codebase, not
  cryptographic secrecy. (See ``DECISIONS.md`` 2026-04-26.)

- Identity is derived from the env-var name (convention over
  configuration). ``BSS_API_TOKEN`` → ``"default"``,
  ``BSS_PORTAL_SELF_SERVE_API_TOKEN`` → ``"portal"``,
  ``BSS_PARTNER_API_TOKEN_ACME`` → ``"partner_acme"``.

- Comparison is constant-time (``hmac.compare_digest``) regardless of
  whether the input matches an entry. Iterating the (small) hashed map
  is fine — the security boundary is the per-entry compare, not the
  outer loop.

- The map is **immutable after load**. Rotation is restart-based;
  per-request ``os.environ`` reads are forbidden by doctrine.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

# Fixed salt for the in-memory hashed token map. Constant-on-disk:
# the salt is not a secret. Its purpose is to make the in-memory
# representation a one-way hash of the env-loaded token so that
# debug-level logging of the map cannot leak the raw token.
_TOKEN_HASH_SALT: Final[bytes] = b"bss-cli-token-map-v0.9-fixed-salt"

_SENTINEL: Final[str] = "changeme"
_MIN_LENGTH: Final[int] = 32

# Identity derived from env-var name. Case-insensitive match.
# ``BSS_API_TOKEN`` → ``default`` (the special-cased single-token
# deployment from v0.3). ``BSS_<NAME>_API_TOKEN`` → ``<name>`` lower.
_DEFAULT_IDENTITY: Final[str] = "default"
_DEFAULT_ENV_VAR: Final[str] = "BSS_API_TOKEN"
_NAMED_PATTERN: Final[re.Pattern[str]] = re.compile(r"^BSS_(.+)_API_TOKEN$")


class TokenMapInvalid(RuntimeError):
    """Raised by validate_token_map() when the loaded map is unusable.

    Surfaces a clear startup failure: missing default identity,
    duplicate tokens, sentinel value, or length violations. Services
    that catch this raise should refuse to boot (the v0.3 fail-fast
    pattern, extended).
    """


@dataclass(frozen=True)
class TokenMap:
    """Immutable hashed-token → service-identity map.

    Constructed by ``load_token_map_from_env`` and consumed by
    ``BSSApiTokenMiddleware``. The mapping is keyed by the
    HMAC-SHA-256 hash of each token (with the fixed salt above);
    the raw token never lives on the dataclass.

    Use ``lookup(raw_token)`` to resolve a presented token to its
    identity. Returns ``None`` if no entry matches. The lookup
    iterates every entry under ``hmac.compare_digest`` so timing
    cannot distinguish "no entry" from "wrong token" within the
    per-entry compare.
    """

    entries: tuple[tuple[bytes, str], ...]
    """Tuple of (token_hash, identity) pairs. Order is insertion order."""

    @property
    def identities(self) -> tuple[str, ...]:
        """All registered identities, in load order. For diagnostics only."""
        return tuple(identity for _, identity in self.entries)

    def lookup(self, presented: str) -> str | None:
        """Return the identity for ``presented`` or ``None``.

        Constant-time per entry. The full map is iterated even on a
        miss so timing does not leak which-if-any entry matched.
        """
        if not presented:
            return None
        h = _hash_token(presented)
        match: str | None = None
        for stored_hash, identity in self.entries:
            # ``compare_digest`` is constant-time within the compare.
            # We do not break early on match — iterating to the end
            # keeps total wall-time independent of which entry matched
            # (or whether one did).
            if hmac.compare_digest(h, stored_hash):
                match = identity
        return match


def _hash_token(token: str) -> bytes:
    """HMAC-SHA-256(salt, token). Returns 32 bytes. Pure / deterministic."""
    return hmac.new(_TOKEN_HASH_SALT, token.encode("utf-8"), hashlib.sha256).digest()


def _identity_from_env_var(name: str) -> str:
    """Derive ``service_identity`` from the env-var name.

    ``BSS_API_TOKEN`` → ``"default"`` (special-cased — v0.3 single-token).
    ``BSS_<NAME>_API_TOKEN`` → ``<name>`` lowercased. ``<NAME>`` may
    contain underscores (``BSS_PARTNER_API_TOKEN_ACME`` → ``partner_acme``).

    Raises ``ValueError`` if the name does not match either pattern.
    """
    if name == _DEFAULT_ENV_VAR:
        return _DEFAULT_IDENTITY
    m = _NAMED_PATTERN.match(name)
    if not m:
        raise ValueError(
            f"env var {name!r} does not match the BSS named-token pattern "
            f"({_DEFAULT_ENV_VAR!r} or BSS_<NAME>_API_TOKEN)"
        )
    return m.group(1).lower()


def load_token_map_from_env(env: Mapping[str, str] | None = None) -> TokenMap:
    """Build a ``TokenMap`` from env vars matching the BSS token pattern.

    Reads ``BSS_API_TOKEN`` (always — identity ``"default"``) plus any
    ``BSS_<NAME>_API_TOKEN`` value present and non-empty. Identity is
    derived from the env-var name via ``_identity_from_env_var``.

    The loader does **not** validate (length, sentinel, uniqueness).
    Call ``validate_token_map`` separately so the failure path
    distinguishes "no default" from "invalid value". This matches the
    v0.3 ``validate_api_token_present`` shape but extends it to the
    multi-token case.

    ``env`` defaults to ``os.environ`` and exists for tests so they
    don't need to ``monkeypatch.setenv`` an arbitrary number of vars.
    """
    source: Mapping[str, str] = env if env is not None else os.environ

    # Insertion order: default first, then named tokens sorted by env-var
    # name so the map is deterministic across processes (the lookup is
    # order-independent, but logging the map should be stable).
    pairs: list[tuple[bytes, str]] = []
    seen_identities: set[str] = set()

    default_value = source.get(_DEFAULT_ENV_VAR, "")
    if default_value:
        pairs.append((_hash_token(default_value), _DEFAULT_IDENTITY))
        seen_identities.add(_DEFAULT_IDENTITY)

    named_vars = sorted(
        name
        for name in source
        if name != _DEFAULT_ENV_VAR and _NAMED_PATTERN.match(name)
    )
    for name in named_vars:
        value = source.get(name, "")
        if not value:
            continue
        identity = _identity_from_env_var(name)
        if identity in seen_identities:
            # Two env vars resolve to the same identity (e.g. someone
            # set both BSS_PORTAL_SELF_SERVE_API_TOKEN and BSS_PORTAL_SELF_SERVE_API_TOKEN_).
            # Reject at validation time, not load time, so the error
            # message can include all conflicts at once.
            pairs.append((_hash_token(value), identity))
            continue
        pairs.append((_hash_token(value), identity))
        seen_identities.add(identity)

    return TokenMap(entries=tuple(pairs))


def validate_token_map(
    token_map: TokenMap,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Enforce the v0.9 doctrine on a loaded ``TokenMap``.

    Rules:

    1. The ``default`` identity must be present (single-token v0.3
       behaviour preserved when no named tokens are configured).
    2. Every identity must be unique.
    3. Every (hashed) token value must be unique — two identities
       must not share a token.
    4. Every raw token must be ≥32 chars and not the ``"changeme"``
       sentinel. We re-read the env to apply rules 4: the map only
       holds hashes, not raw values, so length / sentinel checks
       happen against the source env.

    On any violation, raises ``TokenMapInvalid`` with a message that
    names the offending identity / env var — never echoing the raw
    token value back.
    """
    source: Mapping[str, str] = env if env is not None else os.environ

    identities = token_map.identities
    if _DEFAULT_IDENTITY not in identities:
        raise TokenMapInvalid(
            f"{_DEFAULT_ENV_VAR} is unset; the {_DEFAULT_IDENTITY!r} identity is "
            "required (v0.3 single-token behaviour). Generate a token via: "
            "openssl rand -hex 32"
        )

    # Rule 2 — unique identities
    seen: dict[str, int] = {}
    for ident in identities:
        seen[ident] = seen.get(ident, 0) + 1
    duplicates = sorted(ident for ident, count in seen.items() if count > 1)
    if duplicates:
        raise TokenMapInvalid(
            f"duplicate service_identity values in token map: {duplicates}. "
            "Each BSS_*_API_TOKEN env var must derive a unique identity. "
            "Check for env-var name collisions."
        )

    # Rule 3 — unique token hashes (two identities sharing a token)
    hash_seen: dict[bytes, list[str]] = {}
    for token_hash, identity in token_map.entries:
        hash_seen.setdefault(token_hash, []).append(identity)
    shared = {h: idents for h, idents in hash_seen.items() if len(idents) > 1}
    if shared:
        # Sort the conflicting identity sets for stable error output.
        conflicts = sorted(sorted(idents) for idents in shared.values())
        raise TokenMapInvalid(
            f"identities sharing a token value: {conflicts}. "
            "Each named token must be a distinct random value — sharing one "
            "token across surfaces defeats the blast-radius point of named "
            "tokens. Regenerate via: openssl rand -hex 32"
        )

    # Rule 4 — length + sentinel against the source env. We must look at
    # raw values, which means re-reading env (the map only holds hashes).
    candidates: list[str] = [_DEFAULT_ENV_VAR]
    candidates.extend(
        sorted(
            name for name in source
            if name != _DEFAULT_ENV_VAR and _NAMED_PATTERN.match(name)
        )
    )
    for name in candidates:
        value = source.get(name, "")
        if not value:
            continue
        if value == _SENTINEL:
            raise TokenMapInvalid(
                f"{name} is still the .env.example sentinel value "
                f"({_SENTINEL!r}); replace it with a real token. "
                "Generate via: openssl rand -hex 32"
            )
        if len(value) < _MIN_LENGTH:
            raise TokenMapInvalid(
                f"{name} is too short ({len(value)} chars; need >="
                f"{_MIN_LENGTH}). Generate via: openssl rand -hex 32"
            )


def validate_token_map_present(env: Mapping[str, str] | None = None) -> TokenMap:
    """Load and validate the token map. Returns the validated map.

    Called from each service's lifespan BEFORE any other setup. Raises
    ``TokenMapInvalid`` (or its parent ``RuntimeError``) on misconfig
    so the service refuses to boot. Compose's healthcheck surfaces
    the crash immediately.

    This is the v0.9 successor to ``validate_api_token_present``. The
    old name is preserved as a deprecation alias in ``startup.py``.
    """
    token_map = load_token_map_from_env(env)
    validate_token_map(token_map, env=env)
    return token_map


__all__ = [
    "TokenMap",
    "TokenMapInvalid",
    "load_token_map_from_env",
    "validate_token_map",
    "validate_token_map_present",
]
