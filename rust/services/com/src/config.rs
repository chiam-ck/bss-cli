//! Service settings — port of `app.config.Settings`.

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
    pub som_url: String,
    pub subscription_url: String,
    pub env: String,
    pub tenant_default: String,
    /// v1.1 — COM's own loyalty client (consume lifecycle). Empty → OFF.
    pub loyalty_base_url: String,
    pub loyalty_api_token: String,
    /// v1.2 — resilient pipeline knobs.
    pub mq_max_retries: u32,
    pub mq_retry_backoff_ms: u64,
    pub outbox_relay_interval_ms: u64,
    pub outbox_relay_batch_size: i64,
    pub order_stuck_threshold_seconds: i64,
    pub reconciliation_interval_seconds: u64,
    pub api_token: String,
}

impl Settings {
    pub fn from_env() -> Self {
        Settings {
            service_name: env_or("BSS_SERVICE_NAME", "com"),
            version: env_or("BSS_VERSION", BSS_RELEASE),
            log_level: env_or("BSS_LOG_LEVEL", "INFO"),
            db_url: normalize_db_url(&env_or("BSS_DB_URL", "")),
            mq_url: env_or("BSS_MQ_URL", ""),
            crm_url: env_or("BSS_CRM_URL", "http://crm:8000"),
            catalog_url: env_or("BSS_CATALOG_URL", "http://catalog:8000"),
            payment_url: env_or("BSS_PAYMENT_URL", "http://payment:8000"),
            som_url: env_or("BSS_SOM_URL", "http://som:8000"),
            subscription_url: env_or("BSS_SUBSCRIPTION_URL", "http://subscription:8000"),
            env: env_or("BSS_ENV", "development"),
            tenant_default: env_or("BSS_TENANT_DEFAULT", "DEFAULT"),
            loyalty_base_url: env_or("BSS_LOYALTY_BASE_URL", "http://loyalty-http:8080"),
            loyalty_api_token: env_or("BSS_LOYALTY_API_TOKEN", ""),
            mq_max_retries: env_u64("BSS_MQ_MAX_RETRIES", 5) as u32,
            mq_retry_backoff_ms: env_u64("BSS_MQ_RETRY_BACKOFF_MS", 5000),
            outbox_relay_interval_ms: env_u64("BSS_OUTBOX_RELAY_INTERVAL_MS", 250),
            outbox_relay_batch_size: env_u64("BSS_OUTBOX_RELAY_BATCH_SIZE", 100) as i64,
            order_stuck_threshold_seconds: env_u64("BSS_ORDER_STUCK_THRESHOLD_SECONDS", 900) as i64,
            reconciliation_interval_seconds: env_u64("BSS_RECONCILIATION_INTERVAL_SECONDS", 60),
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
