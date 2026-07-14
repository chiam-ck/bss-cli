//! Portal settings — env-driven. Port of `bss_self_serve.config`. Process env is
//! the source of truth (deployed containers get it from compose `env_file`).
//!
//! Only the fields the current slice consumes are wired to live code; the rest
//! are carried so later slices (signup/KYC/payment/chat) don't re-derive them.

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

/// Resolved portal settings.
#[derive(Debug, Clone)]
pub struct Settings {
    pub service_name: String,
    pub version: String,
    pub log_level: String,

    // Upstream BSS service endpoints.
    pub catalog_url: String,
    pub com_url: String,
    pub subscription_url: String,
    pub crm_url: String,
    pub payment_url: String,
    pub provisioning_url: String,

    pub port: u16,
    pub session_ttl: i64,
    pub db_url: String,

    // KYC (v0.15) — carried for later slices.
    pub kyc_provider: String,
    pub kyc_didit_api_key: String,
    pub kyc_didit_workflow_id: String,
    pub kyc_didit_webhook_secret: String,
    pub public_url: String,

    // Payment (v0.16) — carried for later slices.
    pub payment_provider: String,
    pub payment_stripe_api_key: String,
    pub env: String,

    // Chat (v0.19) — carried for later slices.
    pub operator_support_email: String,
    pub operator_name: Option<String>,
}

impl Settings {
    pub fn from_env() -> Self {
        let operator_name = std::env::var("BSS_OPERATOR_NAME")
            .ok()
            .filter(|v| !v.is_empty());
        Self {
            service_name: env_or("BSS_PORTAL_SELF_SERVE_SERVICE_NAME", "portal-self-serve"),
            version: BSS_RELEASE.to_string(),
            log_level: env_or("BSS_PORTAL_LOG_LEVEL", "INFO"),
            catalog_url: env_or("BSS_CATALOG_URL", "http://catalog:8000"),
            com_url: env_or("BSS_COM_URL", "http://com:8000"),
            subscription_url: env_or("BSS_SUBSCRIPTION_URL", "http://subscription:8000"),
            crm_url: env_or("BSS_CRM_URL", "http://crm:8000"),
            payment_url: env_or("BSS_PAYMENT_URL", "http://payment:8000"),
            provisioning_url: env_or("BSS_PROVISIONING_URL", "http://provisioning:8000"),
            port: env_int("BSS_PORTAL_SELF_SERVE_PORT", 9001) as u16,
            session_ttl: env_int("BSS_PORTAL_SELF_SERVE_SESSION_TTL", 600),
            db_url: env_or("BSS_DB_URL", ""),
            kyc_provider: env_or("BSS_PORTAL_KYC_PROVIDER", "prebaked"),
            kyc_didit_api_key: env_or("BSS_PORTAL_KYC_DIDIT_API_KEY", ""),
            kyc_didit_workflow_id: env_or("BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID", ""),
            kyc_didit_webhook_secret: env_or("BSS_PORTAL_KYC_DIDIT_WEBHOOK_SECRET", ""),
            public_url: env_or("BSS_PORTAL_PUBLIC_URL", "http://localhost:9001"),
            payment_provider: env_or("BSS_PAYMENT_PROVIDER", "mock"),
            payment_stripe_api_key: env_or("BSS_PAYMENT_STRIPE_API_KEY", ""),
            env: env_or("BSS_ENV", "development"),
            operator_support_email: env_or("BSS_OPERATOR_SUPPORT_EMAIL", "support@bss-cli.local"),
            operator_name,
        }
    }
}

impl Default for Settings {
    fn default() -> Self {
        Self::from_env()
    }
}
