"""Orchestrator config — LLM provider + downstream service URLs.

Reads from repo-root ``.env`` via the `_REPO_ROOT` pattern so config loads
regardless of where the CLI is invoked from.

Env vars (prefix ``BSS_``):
    BSS_LLM_BASE_URL       OpenRouter (or OpenAI-compatible) endpoint
    BSS_LLM_MODEL          Model identifier, e.g. ``deepseek/deepseek-v4-pro``
    BSS_LLM_API_KEY        OpenRouter API key (``sk-or-...``)
    BSS_LLM_HTTP_REFERER   OpenRouter attribution header (optional)
    BSS_LLM_APP_NAME       OpenRouter ``X-Title`` header (optional)

Plus one URL per downstream BSS service (defaults aim at docker-compose hosts).
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # ── LLM provider (OpenRouter via openai SDK) ────────────────────────
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "deepseek/deepseek-v4-pro"
    llm_api_key: str = ""
    llm_http_referer: str = "https://github.com/samurai-bot/bss-cli"
    llm_app_name: str = "bss-cli"

    # ── Downstream service URLs (dev defaults point at localhost) ───────
    crm_url: str = "http://localhost:8002"
    catalog_url: str = "http://localhost:8001"
    payment_url: str = "http://localhost:8003"
    com_url: str = "http://localhost:8004"
    som_url: str = "http://localhost:8005"
    subscription_url: str = "http://localhost:8006"
    mediation_url: str = "http://localhost:8007"
    rating_url: str = "http://localhost:8008"
    provisioning_url: str = "http://localhost:8010"

    # ── Misc ────────────────────────────────────────────────────────────
    env: str = "development"
    tenant_default: str = "DEFAULT"
    log_level: str = "INFO"

    # ── v0.12 chat scoping ─────────────────────────────────────────────
    # The orchestrator owns ``audit.chat_usage`` because no single
    # domain service does — the table aggregates costs across CRM /
    # subscription / payment writes that the chat surface drives.
    db_url: str = ""
    chat_rate_per_customer_per_hour: int = 20
    chat_cost_cap_per_customer_per_month_cents: int = 200
    chat_rate_per_ip_per_hour: int = 60

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="BSS_",
        extra="ignore",
    )

    @property
    def llm_actor(self) -> str:
        """X-BSS-Actor value when calls originate from the LLM.

        Derived from the model slug so the audit trail reflects which model
        actually performed the actions (useful when swapping dev → hero model).
        """
        slug = self.llm_model.replace("/", "-")
        return f"llm-{slug}"


settings = Settings()
