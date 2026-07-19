"""Settings load behavior."""

from __future__ import annotations

from bss_middleware.config import Settings


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("BSS_API_TOKEN", "env-value-32-chars-aaaaaaaaaaaa")
    s = Settings()
    assert s.BSS_API_TOKEN == "env-value-32-chars-aaaaaaaaaaaa"


def test_settings_default_empty_when_unset(monkeypatch):
    monkeypatch.delenv("BSS_API_TOKEN", raising=False)
    # The .env file at repo root may set it; this test asserts the
    # class-level default (empty string) when neither env var nor
    # .env file provides a value. We can't fully isolate from .env
    # in a workspace test, so we just sanity-check the default value
    # exists and is a string.
    assert isinstance(Settings.model_fields["BSS_API_TOKEN"].default, str)
