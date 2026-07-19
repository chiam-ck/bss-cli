"""Unit tests for validate_api_token_present()."""

from __future__ import annotations

import pytest
from bss_middleware import TEST_TOKEN, validate_api_token_present


def test_rejects_empty_token(monkeypatch):
    monkeypatch.setenv("BSS_API_TOKEN", "")
    with pytest.raises(RuntimeError, match="unset"):
        validate_api_token_present()


def test_rejects_changeme_sentinel(monkeypatch):
    monkeypatch.setenv("BSS_API_TOKEN", "changeme")
    with pytest.raises(RuntimeError, match="sentinel"):
        validate_api_token_present()


def test_rejects_short_token(monkeypatch):
    monkeypatch.setenv("BSS_API_TOKEN", "shorty")
    with pytest.raises(RuntimeError, match="too short"):
        validate_api_token_present()


def test_rejects_31_char_token(monkeypatch):
    """Boundary: 31 chars rejected (need >=32)."""
    monkeypatch.setenv("BSS_API_TOKEN", "a" * 31)
    with pytest.raises(RuntimeError, match="too short"):
        validate_api_token_present()


def test_accepts_32_char_token(monkeypatch):
    """Boundary: 32 chars accepted."""
    monkeypatch.setenv("BSS_API_TOKEN", "a" * 32)
    validate_api_token_present()  # no raise


def test_accepts_test_token(monkeypatch):
    """The shared TEST_TOKEN constant must satisfy the validator."""
    monkeypatch.setenv("BSS_API_TOKEN", TEST_TOKEN)
    validate_api_token_present()  # no raise
