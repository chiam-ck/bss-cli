//! Service settings — port of `app.config.Settings` (pydantic `BSS_`-prefixed).
//!
//! Fields map to `BSS_<UPPER>` env vars, same as the Python `env_prefix="BSS_"`.
//! `db_url` is normalized to sqlx's plain `postgres://` scheme (the `.env` carries
//! the SQLAlchemy `postgresql+asyncpg://` dialect the async engine wants).

use bss_models::BSS_RELEASE;

#[derive(Debug, Clone)]
pub struct Settings {
    pub service_name: String,
    pub version: String,
    pub log_level: String,
    pub db_url: String,
    pub mq_url: String,
    pub env: String,
    pub tenant_default: String,
    pub catalog_url: String,
    /// The perimeter token this service presents on outbound calls (`api_token()`).
    pub api_token: String,
}

impl Settings {
    /// Read settings from the environment, applying the Python defaults.
    pub fn from_env() -> Self {
        Settings {
            service_name: env_or("BSS_SERVICE_NAME", "rating"),
            version: env_or("BSS_VERSION", BSS_RELEASE),
            log_level: env_or("BSS_LOG_LEVEL", "INFO"),
            db_url: normalize_db_url(&env_or("BSS_DB_URL", "")),
            mq_url: env_or("BSS_MQ_URL", ""),
            env: env_or("BSS_ENV", "development"),
            tenant_default: env_or("BSS_TENANT_DEFAULT", "DEFAULT"),
            catalog_url: env_or("BSS_CATALOG_URL", "http://catalog:8000"),
            api_token: env_or("BSS_API_TOKEN", ""),
        }
    }
}

/// sqlx speaks plain `postgres://` — drop the SQLAlchemy async dialect suffix
/// the `.env` uses (same normalization the conformance harness does).
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
