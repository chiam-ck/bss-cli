"""Settings for bss-telemetry — env-driven, _REPO_ROOT pattern.

In Docker, env vars come from the container environment (compose
``env_file: .env``) and pydantic-settings reads them via os.environ;
the ``env_file`` path resolves into site-packages and is silently
ignored. At local pytest time, the path resolves to the repo root
and the .env is read directly.
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

    BSS_OTEL_ENABLED: bool = True
    BSS_OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://tech-vm:4318"
    BSS_OTEL_EXPORTER_OTLP_PROTOCOL: str = "http/protobuf"
    BSS_OTEL_SERVICE_NAME_PREFIX: str = "bss"
    BSS_OTEL_SAMPLING_RATIO: float = 1.0
    BSS_OTEL_SERVICE_VERSION: str = "0.2.0"
