"""Lifespan validator — pepper presence + sentinel + length."""

from __future__ import annotations

import pytest
from bss_portal_auth import validate_pepper_present


def test_pepper_present_passes_with_real_value(monkeypatch):
    monkeypatch.setenv("BSS_PORTAL_TOKEN_PEPPER", "a" * 64)
    validate_pepper_present()  # no raise


def test_pepper_unset_raises(monkeypatch):
    monkeypatch.setenv("BSS_PORTAL_TOKEN_PEPPER", "")
    with pytest.raises(RuntimeError, match="unset"):
        validate_pepper_present()


def test_pepper_sentinel_raises(monkeypatch):
    monkeypatch.setenv("BSS_PORTAL_TOKEN_PEPPER", "changeme")
    with pytest.raises(RuntimeError, match="sentinel"):
        validate_pepper_present()


def test_pepper_too_short_raises(monkeypatch):
    monkeypatch.setenv("BSS_PORTAL_TOKEN_PEPPER", "a" * 16)
    with pytest.raises(RuntimeError, match="too short"):
        validate_pepper_present()
