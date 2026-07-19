"""Unit tests for token primitives — generation, hashing, timing-safe verify."""

from __future__ import annotations

import hmac
import inspect

import pytest
from bss_portal_auth.tokens import (
    OTP_LENGTH,
    generate_magic_link_token,
    generate_otp,
    generate_session_id,
    generate_step_up_grant,
    hash_token,
    verify_token,
)


def test_otp_is_six_numeric_digits():
    for _ in range(50):
        otp = generate_otp()
        assert len(otp) == OTP_LENGTH == 6
        assert otp.isdigit()


def test_otps_are_random_enough():
    sample = {generate_otp() for _ in range(200)}
    # Birthday-bound check: 200 6-digit codes shouldn't collapse to one value.
    assert len(sample) > 100


def test_magic_link_is_url_safe_32_chars():
    for _ in range(20):
        tok = generate_magic_link_token()
        assert len(tok) == 32
        assert all(c.isalnum() or c in "-_" for c in tok)


def test_session_id_and_grant_share_format():
    assert len(generate_session_id()) == 32
    assert len(generate_step_up_grant()) == 32


def test_hash_is_hex_sha256_64_chars():
    h = hash_token("123456")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_hash_is_deterministic_under_same_pepper():
    a = hash_token("123456", pepper="x" * 32)
    b = hash_token("123456", pepper="x" * 32)
    assert a == b


def test_hash_changes_under_different_pepper():
    a = hash_token("123456", pepper="x" * 32)
    b = hash_token("123456", pepper="y" * 32)
    assert a != b


def test_verify_token_passes_for_correct_code():
    h = hash_token("424242")
    assert verify_token("424242", h) is True


def test_verify_token_rejects_wrong_code():
    h = hash_token("424242")
    assert verify_token("000000", h) is False


def test_verify_token_uses_constant_time_compare():
    """The verify path must call hmac.compare_digest, never `==`.

    Inspect the source of verify_token to assert the canonical
    constant-time idiom is in use. A regression that swaps to ``==``
    would silently downgrade brute-force resistance and is exactly
    the kind of drift this guard catches. Doctrine: V0_8_0.md §1.5.
    """
    src = inspect.getsource(verify_token)
    assert "hmac.compare_digest" in src
    assert hmac.compare_digest is not None  # smoke


def test_hash_raises_when_pepper_unset(monkeypatch):
    """Defensive: missing pepper means HMAC would otherwise key on b''.

    Startup validator catches this in production; this test confirms
    the runtime function ALSO refuses, so a regression in lifespan
    wiring can't silently make every token hash to the same value.
    """
    monkeypatch.setenv("BSS_PORTAL_TOKEN_PEPPER", "")
    with pytest.raises(RuntimeError, match="BSS_PORTAL_TOKEN_PEPPER"):
        hash_token("123456")
