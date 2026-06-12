from pathlib import Path

from bss_models import BSS_RELEASE
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    service_name: str = "som"
    version: str = BSS_RELEASE
    log_level: str = "INFO"
    db_url: str = ""
    mq_url: str = ""
    crm_url: str = "http://crm:8000"
    env: str = "development"
    tenant_default: str = "DEFAULT"

    # v1.2 — outbox relay knobs + safe-consumer retry budget.
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


settings = Settings()
