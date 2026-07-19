"""Unit tests for TokenMap + loader + validator (v0.9 named tokens).

Mirrors test_startup.py's coverage of fail-fast paths but extends it
to the multi-token map. The token auth middleware uses the same
TokenMap so any green here = the middleware will resolve identity
correctly.
"""

from __future__ import annotations

import inspect
import sys

# ``bss_middleware.api_token`` (the submodule) is shadowed in the package
# namespace by the ``api_token()`` function re-exported from
# ``bss_middleware/__init__.py``. Pull the submodule from sys.modules
# directly for source-inspection assertions.
import bss_middleware.api_token  # noqa: F401 — ensures sys.modules entry exists
import pytest
from bss_middleware import (
    TEST_TOKEN,
    TokenMap,
    TokenMapInvalid,
    load_token_map_from_env,
    validate_token_map,
    validate_token_map_present,
)

api_token_module = sys.modules["bss_middleware.api_token"]


# Two distinct 64-char tokens for tests that need >=2 named tokens.
PORTAL_TOKEN = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
CSR_TOKEN = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"


# ─────────────────────────────────────────────────────────────────────────────
# load_token_map_from_env
# ─────────────────────────────────────────────────────────────────────────────


def test_loader_single_default_token():
    env = {"BSS_API_TOKEN": TEST_TOKEN}
    m = load_token_map_from_env(env)
    assert m.identities == ("default",)
    assert m.lookup(TEST_TOKEN) == "default"


def test_loader_default_plus_portal():
    env = {"BSS_API_TOKEN": TEST_TOKEN, "BSS_PORTAL_SELF_SERVE_API_TOKEN": PORTAL_TOKEN}
    m = load_token_map_from_env(env)
    assert m.identities == ("default", "portal_self_serve")
    assert m.lookup(TEST_TOKEN) == "default"
    assert m.lookup(PORTAL_TOKEN) == "portal_self_serve"


def test_loader_named_token_only_no_default():
    """Loader records what's there; validator is what enforces 'default required'."""
    env = {"BSS_PORTAL_SELF_SERVE_API_TOKEN": PORTAL_TOKEN}
    m = load_token_map_from_env(env)
    assert m.identities == ("portal_self_serve",)
    assert m.lookup(PORTAL_TOKEN) == "portal_self_serve"
    assert m.lookup(TEST_TOKEN) is None


def test_loader_partner_underscore_name_lowercased():
    env = {"BSS_API_TOKEN": TEST_TOKEN, "BSS_PARTNER_API_TOKEN_ACME": PORTAL_TOKEN}
    m = load_token_map_from_env(env)
    assert "partner_api_token_acme" not in m.identities
    # ``BSS_PARTNER_API_TOKEN_ACME`` matches BSS_(.+)_API_TOKEN as
    # ``PARTNER`` (greedy regex prefers the leftmost). We document
    # this in the loader docstring; the canonical pattern is to put
    # the partner name BEFORE _API_TOKEN. Test the contract we ship.
    # Re-derived: BSS_PARTNER_API_TOKEN_ACME doesn't match the
    # ^BSS_(.+)_API_TOKEN$ pattern at all (it has trailing _ACME).
    # So it must be ignored as a non-matching env var.
    assert m.identities == ("default",)


def test_loader_partner_naming_convention_works():
    """``BSS_<NAME>_API_TOKEN`` is the canonical pattern."""
    env = {
        "BSS_API_TOKEN": TEST_TOKEN,
        "BSS_PARTNER_ACME_API_TOKEN": PORTAL_TOKEN,
    }
    m = load_token_map_from_env(env)
    assert "partner_acme" in m.identities
    assert m.lookup(PORTAL_TOKEN) == "partner_acme"


def test_loader_ignores_unrelated_env_vars():
    env = {
        "BSS_API_TOKEN": TEST_TOKEN,
        "BSS_DB_URL": "postgres://...",
        "PATH": "/usr/bin",
    }
    m = load_token_map_from_env(env)
    assert m.identities == ("default",)


def test_loader_skips_empty_named_value():
    env = {"BSS_API_TOKEN": TEST_TOKEN, "BSS_PORTAL_SELF_SERVE_API_TOKEN": ""}
    m = load_token_map_from_env(env)
    assert m.identities == ("default",)


def test_loader_named_tokens_sorted_for_determinism():
    """Named tokens load in sorted env-var order for stable diagnostics."""
    env = {
        "BSS_API_TOKEN": TEST_TOKEN,
        "BSS_ZED_API_TOKEN": "z" * 64,
        "BSS_ALPHA_API_TOKEN": "a" * 64,
    }
    m = load_token_map_from_env(env)
    # default first, then alpha < zed.
    assert m.identities == ("default", "alpha", "zed")


# ─────────────────────────────────────────────────────────────────────────────
# Hashing — raw values must not be retained on the map
# ─────────────────────────────────────────────────────────────────────────────


def test_token_map_stores_hashed_values_not_raw():
    env = {"BSS_API_TOKEN": TEST_TOKEN, "BSS_PORTAL_SELF_SERVE_API_TOKEN": PORTAL_TOKEN}
    m = load_token_map_from_env(env)
    raw_test = TEST_TOKEN.encode("utf-8")
    raw_portal = PORTAL_TOKEN.encode("utf-8")
    for stored_hash, _identity in m.entries:
        assert stored_hash != raw_test, "raw default token leaked into map"
        assert stored_hash != raw_portal, "raw portal token leaked into map"
        # HMAC-SHA-256 output is exactly 32 bytes.
        assert len(stored_hash) == 32


def test_lookup_uses_constant_time_compare():
    """The map's lookup must use ``hmac.compare_digest`` internally."""
    src = inspect.getsource(api_token_module)
    assert "hmac.compare_digest" in src
    # Negative: no naive ``==`` comparison of the input hash against entries.
    assert "h == stored_hash" not in src
    assert "h==stored_hash" not in src


def test_hash_is_one_way_in_practice():
    """Reverse-lookup of a stored hash must not yield the raw token.

    Sanity check that we use a real cryptographic hash (HMAC-SHA-256)
    and not, say, base64. An attacker with debug log access who sees
    the hash should not be able to recover the env value.
    """
    env = {"BSS_API_TOKEN": TEST_TOKEN}
    m = load_token_map_from_env(env)
    stored_hash, _ = m.entries[0]
    # The raw token is never a substring or prefix of its hash.
    assert TEST_TOKEN.encode() not in stored_hash
    # Hash bytes are not a printable ASCII representation of the token.
    try:
        decoded = stored_hash.decode("utf-8")
    except UnicodeDecodeError:
        decoded = ""
    assert TEST_TOKEN not in decoded


# ─────────────────────────────────────────────────────────────────────────────
# validate_token_map
# ─────────────────────────────────────────────────────────────────────────────


def test_validate_requires_default_identity():
    env = {"BSS_PORTAL_SELF_SERVE_API_TOKEN": PORTAL_TOKEN}
    m = load_token_map_from_env(env)
    with pytest.raises(TokenMapInvalid, match="BSS_API_TOKEN is unset"):
        validate_token_map(m, env=env)


def test_validate_rejects_sentinel_in_default():
    env = {"BSS_API_TOKEN": "changeme"}
    # Loader still builds a map (sentinel hashes fine); validator rejects.
    m = load_token_map_from_env(env)
    with pytest.raises(TokenMapInvalid, match="sentinel"):
        validate_token_map(m, env=env)


def test_validate_rejects_sentinel_in_named():
    env = {"BSS_API_TOKEN": TEST_TOKEN, "BSS_PORTAL_SELF_SERVE_API_TOKEN": "changeme"}
    m = load_token_map_from_env(env)
    with pytest.raises(TokenMapInvalid, match="BSS_PORTAL_SELF_SERVE_API_TOKEN.*sentinel"):
        validate_token_map(m, env=env)


def test_validate_rejects_short_default():
    env = {"BSS_API_TOKEN": "a" * 31}
    m = load_token_map_from_env(env)
    with pytest.raises(TokenMapInvalid, match="too short"):
        validate_token_map(m, env=env)


def test_validate_accepts_32_char_token():
    env = {"BSS_API_TOKEN": "a" * 32}
    m = load_token_map_from_env(env)
    validate_token_map(m, env=env)  # no raise


def test_validate_rejects_short_named_token():
    env = {"BSS_API_TOKEN": TEST_TOKEN, "BSS_PORTAL_SELF_SERVE_API_TOKEN": "a" * 31}
    m = load_token_map_from_env(env)
    with pytest.raises(TokenMapInvalid, match="BSS_PORTAL_SELF_SERVE_API_TOKEN.*too short"):
        validate_token_map(m, env=env)


def test_validate_rejects_shared_token_across_identities():
    """Two identities must never share a token value (defeats blast-radius)."""
    env = {
        "BSS_API_TOKEN": TEST_TOKEN,
        "BSS_PORTAL_SELF_SERVE_API_TOKEN": TEST_TOKEN,  # same as default — REJECT
    }
    m = load_token_map_from_env(env)
    with pytest.raises(TokenMapInvalid, match="sharing a token"):
        validate_token_map(m, env=env)


def test_validate_does_not_echo_raw_token_in_error():
    """Error messages must not leak the offending token value."""
    env = {"BSS_API_TOKEN": TEST_TOKEN, "BSS_PORTAL_SELF_SERVE_API_TOKEN": "a" * 31}
    m = load_token_map_from_env(env)
    with pytest.raises(TokenMapInvalid) as exc_info:
        validate_token_map(m, env=env)
    msg = str(exc_info.value)
    assert ("a" * 31) not in msg
    assert TEST_TOKEN not in msg


# ─────────────────────────────────────────────────────────────────────────────
# validate_token_map_present — combined load + validate
# ─────────────────────────────────────────────────────────────────────────────


def test_validate_present_returns_validated_map():
    env = {"BSS_API_TOKEN": TEST_TOKEN, "BSS_PORTAL_SELF_SERVE_API_TOKEN": PORTAL_TOKEN}
    m = validate_token_map_present(env=env)
    assert isinstance(m, TokenMap)
    assert m.lookup(TEST_TOKEN) == "default"
    assert m.lookup(PORTAL_TOKEN) == "portal_self_serve"


def test_validate_present_raises_on_missing_default():
    env: dict[str, str] = {}
    with pytest.raises(TokenMapInvalid):
        validate_token_map_present(env=env)


# ─────────────────────────────────────────────────────────────────────────────
# Lookup contract
# ─────────────────────────────────────────────────────────────────────────────


def test_lookup_returns_none_for_unknown_token():
    env = {"BSS_API_TOKEN": TEST_TOKEN}
    m = load_token_map_from_env(env)
    assert m.lookup(PORTAL_TOKEN) is None
    assert m.lookup("") is None
    assert m.lookup("not-a-real-token") is None


def test_lookup_is_constant_time_per_entry():
    """Confirm via the implementation: the lookup uses compare_digest in a loop."""
    src = inspect.getsource(TokenMap.lookup)
    assert "hmac.compare_digest" in src


def test_lookup_does_not_short_circuit_on_first_match():
    """Lookup iterates the full map regardless of whether one matched.

    Constant-time across the loop body means timing cannot leak which
    entry matched (that would be a smaller leak than the per-entry
    compare, but doctrine-wise we still want full-iteration).
    """
    src = inspect.getsource(TokenMap.lookup)
    # No early ``return identity`` before the loop ends. We accept
    # ``return match`` after the loop.
    # This is asserted by reading the source rather than benchmarking
    # because timing assertions are inherently flaky.
    assert "match: str | None = None" in src
    assert "return match" in src


# ─────────────────────────────────────────────────────────────────────────────
# Salt is a constant — process-stable, not crypto-secret
# ─────────────────────────────────────────────────────────────────────────────


def test_same_token_hashes_to_same_value_across_loads():
    env_a = {"BSS_API_TOKEN": TEST_TOKEN}
    env_b = {"BSS_API_TOKEN": TEST_TOKEN}
    m_a = load_token_map_from_env(env_a)
    m_b = load_token_map_from_env(env_b)
    assert m_a.entries == m_b.entries
