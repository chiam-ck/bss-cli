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
    pub env: String,
    pub tenant_default: String,
    /// v1.2 — outbox relay knobs + safe-consumer retry budget.
    pub outbox_relay_interval_ms: u64,
    pub outbox_relay_batch_size: i64,
    pub mq_max_retries: u32,
    pub mq_retry_backoff_ms: u64,
    pub api_token: String,
}

impl Settings {
    pub fn from_env() -> Self {
        Settings {
            service_name: env_or("BSS_SERVICE_NAME", "som"),
            version: env_or("BSS_VERSION", BSS_RELEASE),
            log_level: env_or("BSS_LOG_LEVEL", "INFO"),
            db_url: normalize_db_url(&env_or("BSS_DB_URL", "")),
            mq_url: env_or("BSS_MQ_URL", ""),
            crm_url: env_or("BSS_CRM_URL", "http://crm:8000"),
            env: env_or("BSS_ENV", "development"),
            tenant_default: env_or("BSS_TENANT_DEFAULT", "DEFAULT"),
            outbox_relay_interval_ms: env_u64("BSS_OUTBOX_RELAY_INTERVAL_MS", 250),
            outbox_relay_batch_size: env_u64("BSS_OUTBOX_RELAY_BATCH_SIZE", 100) as i64,
            mq_max_retries: env_u64("BSS_MQ_MAX_RETRIES", 5) as u32,
            mq_retry_backoff_ms: env_u64("BSS_MQ_RETRY_BACKOFF_MS", 5000),
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
