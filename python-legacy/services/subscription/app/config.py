from pathlib import Path

from bss_models import BSS_RELEASE
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    service_name: str = "subscription"
    version: str = BSS_RELEASE
    log_level: str = "INFO"
    db_url: str = ""
    mq_url: str = ""
    env: str = "development"
    tenant_default: str = "DEFAULT"
    crm_url: str = "http://crm:8000"
    payment_url: str = "http://payment:8000"
    catalog_url: str = "http://catalog:8000"
    # v0.18 — in-process renewal worker tick interval. 0 disables.
    # Documented in .env.example. The lifespan reads via os.environ
    # (single int() site at startup); this declaration documents the
    # surface for IDE autocomplete + any future config-show tool.
    renewal_tick_seconds: int = 60
    # v1.2 — outbox relay knobs (the single publisher of staged events) +
    # safe-consumer retry budget for the usage.rated consumer.
    outbox_relay_interval_ms: int = 250
    outbox_relay_batch_size: int = 100
    mq_max_retries: int = 5
    mq_retry_backoff_ms: int = 5000

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="BSS_",
        extra="ignore",
    )
