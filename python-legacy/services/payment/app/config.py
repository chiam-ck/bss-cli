from pathlib import Path

from bss_models import BSS_RELEASE
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    service_name: str = "payment"
    version: str = BSS_RELEASE
    log_level: str = "INFO"
    db_url: str = ""
    mq_url: str = ""
    env: str = "development"
    tenant_default: str = "DEFAULT"
    crm_url: str = "http://crm:8000"
    enable_dev_tokenizer: bool = False

    # ── v0.16: payment provider seam ─────────────────────────────────
    # `mock` (default; in-process MockTokenizerAdapter, preserves the
    # FAIL/DECLINE test affordances) or `stripe` (StripeTokenizerAdapter
    # against Stripe's REST API). Selected at lifespan startup via
    # select_tokenizer; misconfig fails-fast.
    payment_provider: str = "mock"
    payment_stripe_api_key: str = ""
    payment_stripe_publishable_key: str = ""
    payment_stripe_webhook_secret: str = ""
    # Sandbox-only — refused at startup if paired with sk_live_*.
    # Lets the same Stripe test pm_* re-attach to multiple BSS
    # customers without tripping payment_method_already_attached.
    payment_allow_test_card_reuse: bool = False

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="BSS_",
        extra="ignore",
    )
