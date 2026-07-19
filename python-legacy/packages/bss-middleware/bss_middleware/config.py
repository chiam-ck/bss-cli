"""Settings for bss-middleware — env-driven, _REPO_ROOT pattern.

Mirrors packages/bss-telemetry/config.py: in Docker the env vars
come from the container environment via compose ``env_file: .env``;
the ``env_file=`` path resolves into site-packages and is silently
skipped. At local pytest time the path resolves to the repo root
and ``.env`` is read directly.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    BSS_API_TOKEN: str = ""
