//! Service settings — port of `app.config.Settings` (`BSS_`-prefixed).
//!
//! Payment is HTTP-only (no MQ, no consumer, no relay): its `publisher.publish`
//! stages the `audit.domain_event` row with `published_to_mq = false` and returns
//! — nothing in this service connects to RabbitMQ. The v0.16 tokenizer seam is
//! resolved once at startup via [`crate::select::select_tokenizer`].

use bss_models::BSS_RELEASE;

#[derive(Debug, Clone)]
pub struct Settings {
    pub service_name: String,
    pub version: String,
    pub log_level: String,
    pub db_url: String,
    pub env: String,
    pub tenant_default: String,
    pub crm_url: String,
    // ── v0.16 payment provider seam ──────────────────────────────────
    pub payment_provider: String,
    pub payment_stripe_api_key: String,
    pub payment_stripe_publishable_key: String,
    pub payment_stripe_webhook_secret: String,
    pub payment_allow_test_card_reuse: bool,
    pub api_token: String,
}

impl Settings {
    pub fn from_env() -> Self {
        Settings {
            service_name: env_or("BSS_SERVICE_NAME", "payment"),
            version: env_or("BSS_VERSION", BSS_RELEASE),
            log_level: env_or("BSS_LOG_LEVEL", "INFO"),
            db_url: normalize_db_url(&env_or("BSS_DB_URL", "")),
            env: env_or("BSS_ENV", "development"),
            tenant_default: env_or("BSS_TENANT_DEFAULT", "DEFAULT"),
            crm_url: env_or("BSS_CRM_URL", "http://crm:8000"),
            payment_provider: env_or("BSS_PAYMENT_PROVIDER", "mock"),
            payment_stripe_api_key: env_or("BSS_PAYMENT_STRIPE_API_KEY", ""),
            payment_stripe_publishable_key: env_or("BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY", ""),
            payment_stripe_webhook_secret: env_or("BSS_PAYMENT_STRIPE_WEBHOOK_SECRET", ""),
            payment_allow_test_card_reuse: env_bool("BSS_PAYMENT_ALLOW_TEST_CARD_REUSE", false),
            api_token: env_or("BSS_API_TOKEN", ""),
        }
    }
}

/// sqlx speaks plain `postgres://` — drop the SQLAlchemy async dialect suffix.
pub fn normalize_db_url(raw: &str) -> String {
    raw.replace("postgresql+asyncpg://", "postgres://")
        .replace("postgresql://", "postgres://")
}

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn env_bool(key: &str, default: bool) -> bool {
    std::env::var(key)
        .ok()
        .map(|v| matches!(v.to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on"))
        .unwrap_or(default)
}
