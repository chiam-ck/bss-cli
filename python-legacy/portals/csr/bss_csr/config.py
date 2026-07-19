"""CSR portal settings — env-driven, _REPO_ROOT pattern."""

from __future__ import annotations

from pathlib import Path

from bss_models import BSS_RELEASE
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    service_name: str = "portal-csr"
    version: str = BSS_RELEASE
    log_level: str = "INFO"

    # Upstream BSS service endpoints. Reads go direct via bss-clients;
    # writes flow through the orchestrator-mediated cockpit chat
    # (astream_once) — see routes/cockpit.py.
    catalog_url: str = "http://catalog:8000"
    com_url: str = "http://com:8000"
    crm_url: str = "http://crm:8000"
    payment_url: str = "http://payment:8000"
    subscription_url: str = "http://subscription:8000"

    # Port the browser veneer binds to. v0.13: no auth — the cockpit
    # runs single-operator-by-design behind a secure perimeter
    # (CLAUDE.md anti-pattern, DECISIONS 2026-05-01).
    bss_portal_csr_port: int = 9002
