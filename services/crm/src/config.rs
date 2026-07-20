//! Service settings — port of `app.config.Settings`.

use bss_models::BSS_RELEASE;

#[derive(Debug, Clone)]
pub struct Settings {
    pub service_name: String,
    pub version: String,
    pub log_level: String,
    pub db_url: String,
    pub env: String,
    pub tenant_default: String,
    pub subscription_url: String,
    /// v1.1.1 — loyalty customer-registry sync. Empty token → sync OFF.
    pub loyalty_base_url: String,
    pub loyalty_api_token: String,
    /// v0.17 — MSISDN pool low-watermark (emits `inventory.msisdn.pool_low`).
    pub msisdn_pool_low_threshold: i64,
    /// v-reservation phase 4 — open-order expiry sweep cadence, seconds.
    /// 0 disables the in-process worker (tests / external tick driver).
    pub open_order_sweep_seconds: i64,
    pub api_token: String,
}

impl Settings {
    pub fn from_env() -> Self {
        Settings {
            service_name: env_or("BSS_SERVICE_NAME", "crm"),
            version: env_or("BSS_VERSION", BSS_RELEASE),
            log_level: env_or("BSS_LOG_LEVEL", "INFO"),
            db_url: normalize_db_url(&env_or("BSS_DB_URL", "")),
            env: env_or("BSS_ENV", "development"),
            tenant_default: env_or("BSS_TENANT_DEFAULT", "DEFAULT"),
            subscription_url: env_or("BSS_SUBSCRIPTION_URL", "http://subscription:8000"),
            loyalty_base_url: env_or("BSS_LOYALTY_BASE_URL", "http://loyalty-http:8080"),
            loyalty_api_token: env_or("BSS_LOYALTY_API_TOKEN", ""),
            msisdn_pool_low_threshold: env_i64("BSS_INVENTORY_MSISDN_POOL_LOW_THRESHOLD", 50),
            open_order_sweep_seconds: env_i64("BSS_OPEN_ORDER_SWEEP_SECONDS", 300),
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

fn env_i64(key: &str, default: i64) -> i64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}
