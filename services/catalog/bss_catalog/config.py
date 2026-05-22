from pathlib import Path

from bss_models import BSS_RELEASE
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    service_name: str = "catalog"
    version: str = BSS_RELEASE
    log_level: str = "INFO"
    db_url: str = ""
    env: str = "development"
    tenant_default: str = "DEFAULT"

    # v1.1 — loyalty-cli integration. Catalog holds the LoyaltyClient (the
    # token never leaves this process). base_url defaults to the bundled
    # same-network service name; BYOI overrides via BSS_LOYALTY_BASE_URL.
    loyalty_base_url: str = "http://loyalty-http:8080"
    loyalty_api_token: str = ""

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="BSS_",
        extra="ignore",
    )
