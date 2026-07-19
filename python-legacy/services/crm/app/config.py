from pathlib import Path

from bss_models import BSS_RELEASE
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    service_name: str = "crm"
    version: str = BSS_RELEASE
    log_level: str = "INFO"
    db_url: str = ""
    mq_url: str = ""
    env: str = "development"
    tenant_default: str = "DEFAULT"
    subscription_url: str = "http://subscription:8000"

    # v1.1.1 — CRM mirrors new customers into loyalty's registry so its
    # customer-facing views recognise BSS ids. Token never leaves CRM.
    loyalty_base_url: str = "http://loyalty-http:8080"
    loyalty_api_token: str = ""

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="BSS_",
        extra="ignore",
    )
