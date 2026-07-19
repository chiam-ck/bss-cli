//! Service settings — port of `app.config.Settings`.
//!
//! Note: the InventoryClient is pointed at `crm_url` (inventory is hosted inside
//! the CRM service under `/inventory-api/v1/`), exactly as the Python lifespan
//! builds `InventoryClient(base_url=settings.crm_url)`.

use bss_models::BSS_RELEASE;

#[derive(Debug, Clone)]
pub struct Settings {
    pub service_name: String,
    pub version: String,
    pub log_level: String,
    pub db_url: String,
    pub mq_url: String,
    pub crm_url: String,
    pub catalog_url: String,
    pub payment_url: String,
    pub env: String,
    pub tenant_default: String,
    /// v1.2 — resilient pipeline knobs.
    pub mq_max_retries: u32,
    pub mq_retry_backoff_ms: u64,
    pub outbox_relay_interval_ms: u64,
    pub outbox_relay_batch_size: i64,
    /// v0.18 — in-process renewal worker tick interval (seconds). 0 disables.
    pub renewal_tick_seconds: u64,
    /// v0.18 — upcoming-renewal reminder lookahead window (seconds). 0 disables
    /// the reminder sweep (renewal sweep keeps running).
    pub renewal_reminder_lookahead_seconds: i64,
    pub api_token: String,
}

impl Settings {
    pub fn from_env() -> Self {
        Settings {
            service_name: env_or("BSS_SERVICE_NAME", "subscription"),
            version: env_or("BSS_VERSION", BSS_RELEASE),
            log_level: env_or("BSS_LOG_LEVEL", "INFO"),
            db_url: normalize_db_url(&env_or("BSS_DB_URL", "")),
            mq_url: env_or("BSS_MQ_URL", ""),
            crm_url: env_or("BSS_CRM_URL", "http://crm:8000"),
            catalog_url: env_or("BSS_CATALOG_URL", "http://catalog:8000"),
            payment_url: env_or("BSS_PAYMENT_URL", "http://payment:8000"),
            env: env_or("BSS_ENV", "development"),
            tenant_default: env_or("BSS_TENANT_DEFAULT", "DEFAULT"),
            mq_max_retries: env_u64("BSS_MQ_MAX_RETRIES", 5) as u32,
            mq_retry_backoff_ms: env_u64("BSS_MQ_RETRY_BACKOFF_MS", 5000),
            outbox_relay_interval_ms: env_u64("BSS_OUTBOX_RELAY_INTERVAL_MS", 250),
            outbox_relay_batch_size: env_u64("BSS_OUTBOX_RELAY_BATCH_SIZE", 100) as i64,
            renewal_tick_seconds: env_u64("BSS_RENEWAL_TICK_SECONDS", 60),
            renewal_reminder_lookahead_seconds: env_i64(
                "BSS_RENEWAL_REMINDER_LOOKAHEAD_SECONDS",
                24 * 60 * 60,
            ),
            api_token: env_or("BSS_API_TOKEN", ""),
        }
    }
}

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

fn env_u64(key: &str, default: u64) -> u64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn env_i64(key: &str, default: i64) -> i64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}
