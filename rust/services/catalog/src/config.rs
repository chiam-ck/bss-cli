//! Service settings — port of `bss_catalog.config.Settings` (`BSS_`-prefixed).
//!
//! Catalog is HTTP-only (no MQ, no consumer). It holds an optional loyalty
//! client: when `BSS_LOYALTY_API_TOKEN` is unset the promo subsystem is simply
//! OFF (the rest of the catalog still serves).

use bss_models::BSS_RELEASE;

#[derive(Debug, Clone)]
pub struct Settings {
    pub service_name: String,
    pub version: String,
    pub log_level: String,
    pub db_url: String,
    pub env: String,
    pub tenant_default: String,
    /// loyalty-cli base URL (BYOI override; bundled default is the service name).
    pub loyalty_base_url: String,
    /// Bearer token for loyalty. Empty → promo subsystem OFF.
    pub loyalty_api_token: String,
}

impl Settings {
    pub fn from_env() -> Self {
        Settings {
            service_name: env_or("BSS_SERVICE_NAME", "catalog"),
            version: env_or("BSS_VERSION", BSS_RELEASE),
            log_level: env_or("BSS_LOG_LEVEL", "INFO"),
            db_url: normalize_db_url(&env_or("BSS_DB_URL", "")),
            env: env_or("BSS_ENV", "development"),
            tenant_default: env_or("BSS_TENANT_DEFAULT", "DEFAULT"),
            loyalty_base_url: env_or("BSS_LOYALTY_BASE_URL", "http://loyalty-http:8080"),
            loyalty_api_token: env_or("BSS_LOYALTY_API_TOKEN", ""),
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
