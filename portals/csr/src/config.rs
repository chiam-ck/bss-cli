//! Cockpit portal settings — env-driven. Port of `bss_csr.config`.
//!
//! Process env is the source of truth (deployed containers get it from compose
//! `env_file`). **No auth settings**: v0.13 retired the CSR login — the cockpit
//! runs single-operator-by-design behind a secure perimeter (CLAUDE.md
//! anti-pattern, DECISIONS 2026-05-01). The operator's `actor` comes from
//! `.bss-cli/settings.toml` via `bss_cockpit::config::current()`, not from here.

use bss_models::BSS_RELEASE;

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn env_int(key: &str, default: i64) -> i64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

/// Resolved cockpit settings.
#[derive(Debug, Clone)]
pub struct Settings {
    pub service_name: String,
    pub version: String,
    pub log_level: String,

    // Upstream BSS service endpoints. Reads go direct via bss-clients; narrative
    // work flows through the orchestrator-mediated cockpit chat.
    pub catalog_url: String,
    pub com_url: String,
    pub crm_url: String,
    pub payment_url: String,
    pub subscription_url: String,
    /// The chat tool registry needs this (`usage.*`); no CRM screen calls it.
    pub mediation_url: String,
    pub som_url: String,
    pub inventory_url: String,
    pub provisioning_url: String,

    /// Port the browser veneer binds to.
    pub port: u16,
    pub db_url: String,
    pub env: String,
}

impl Settings {
    pub fn from_env() -> Self {
        Self {
            service_name: env_or("BSS_PORTAL_CSR_SERVICE_NAME", "portal-csr"),
            version: BSS_RELEASE.to_string(),
            log_level: env_or("BSS_PORTAL_LOG_LEVEL", "INFO"),
            catalog_url: env_or("BSS_CATALOG_URL", "http://catalog:8000"),
            com_url: env_or("BSS_COM_URL", "http://com:8000"),
            crm_url: env_or("BSS_CRM_URL", "http://crm:8000"),
            payment_url: env_or("BSS_PAYMENT_URL", "http://payment:8000"),
            subscription_url: env_or("BSS_SUBSCRIPTION_URL", "http://subscription:8000"),
            mediation_url: env_or("BSS_MEDIATION_URL", "http://mediation:8000"),
            som_url: env_or("BSS_SOM_URL", "http://som:8000"),
            inventory_url: env_or("BSS_INVENTORY_URL", "http://inventory:8000"),
            provisioning_url: env_or("BSS_PROVISIONING_URL", "http://provisioning:8000"),
            port: env_int("BSS_PORTAL_CSR_PORT", 9002) as u16,
            db_url: env_or("BSS_DB_URL", ""),
            env: env_or("BSS_ENV", "development"),
        }
    }
}

impl Default for Settings {
    fn default() -> Self {
        Self::from_env()
    }
}
