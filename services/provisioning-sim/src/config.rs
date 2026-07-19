//! Service settings — port of `app.config.Settings` (pydantic `BSS_`-prefixed).

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
    /// `BSS_ESIM_PROVIDER` — `sim` (default, fully works) | `onbglobal` |
    /// `esim_access` (accepted at startup, raise on first use).
    pub esim_provider: String,
}

impl Settings {
    pub fn from_env() -> Self {
        Settings {
            service_name: env_or("BSS_SERVICE_NAME", "provisioning-sim"),
            version: env_or("BSS_VERSION", BSS_RELEASE),
            log_level: env_or("BSS_LOG_LEVEL", "INFO"),
            db_url: normalize_db_url(&env_or("BSS_DB_URL", "")),
            mq_url: env_or("BSS_MQ_URL", ""),
            env: env_or("BSS_ENV", "development"),
            tenant_default: env_or("BSS_TENANT_DEFAULT", "DEFAULT"),
            esim_provider: env_or("BSS_ESIM_PROVIDER", "sim"),
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
