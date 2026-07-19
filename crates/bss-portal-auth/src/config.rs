//! bss-portal-auth settings — env-driven. Port of `bss_portal_auth.config`.
//!
//! Python uses `pydantic-settings` reading the repo-root `.env`; in Rust the
//! process env is the source of truth (deployed containers get it from compose
//! `env_file`; local dev/tests `set -a; source .env; set +a`), matching every
//! other Rust crate's `Settings::from_env`. Env prefix `BSS_PORTAL_`.
//!
//! The pepper (`BSS_PORTAL_TOKEN_PEPPER`) is never logged. TTLs + rate-limit
//! scalars default to `V0_8_0.md §1.3` and are consumed by the DB service layer
//! (later sub-slices).

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

/// Resolved portal-auth settings. Field names mirror the Python `Settings`
/// attributes (upper-snake env vars → lower-snake fields).
#[derive(Debug, Clone)]
pub struct Settings {
    /// Server-side pepper for HMAC-ing OTP / magic-link / step-up tokens.
    /// Never logged. Rotation invalidates all in-flight tokens.
    pub token_pepper: String,
    /// Public-facing base URL for outbound email links. No trailing slash.
    pub public_url: String,
    pub email_provider: String,
    /// Deprecated alias for `email_provider`.
    pub email_adapter: String,
    pub dev_mailbox_path: String,
    pub email_resend_api_key: String,
    pub email_resend_webhook_secret: String,
    pub email_from: String,
    pub dev_insecure_cookie: i64,
    pub login_token_ttl_s: i64,
    pub stepup_token_ttl_s: i64,
    pub stepup_grant_ttl_s: i64,
    pub stepup_pending_ttl_s: i64,
    pub session_ttl_s: i64,
    pub login_per_email_max: i64,
    pub login_per_email_window_s: i64,
    pub login_per_ip_max: i64,
    pub login_per_ip_window_s: i64,
    pub verify_per_email_max: i64,
    pub verify_per_email_window_s: i64,
    pub stepup_per_session_max: i64,
    pub stepup_per_session_window_s: i64,
}

impl Settings {
    pub fn from_env() -> Self {
        Self {
            token_pepper: env_or("BSS_PORTAL_TOKEN_PEPPER", ""),
            public_url: env_or("BSS_PORTAL_PUBLIC_URL", ""),
            email_provider: env_or("BSS_PORTAL_EMAIL_PROVIDER", ""),
            email_adapter: env_or("BSS_PORTAL_EMAIL_ADAPTER", ""),
            dev_mailbox_path: env_or("BSS_PORTAL_DEV_MAILBOX_PATH", "/tmp/bss-portal-mailbox.log"),
            email_resend_api_key: env_or("BSS_PORTAL_EMAIL_RESEND_API_KEY", ""),
            email_resend_webhook_secret: env_or("BSS_PORTAL_EMAIL_RESEND_WEBHOOK_SECRET", ""),
            email_from: env_or("BSS_PORTAL_EMAIL_FROM", ""),
            dev_insecure_cookie: env_int("BSS_PORTAL_DEV_INSECURE_COOKIE", 0),
            login_token_ttl_s: env_int("BSS_PORTAL_LOGIN_TOKEN_TTL_S", 15 * 60),
            stepup_token_ttl_s: env_int("BSS_PORTAL_STEPUP_TOKEN_TTL_S", 5 * 60),
            stepup_grant_ttl_s: env_int("BSS_PORTAL_STEPUP_GRANT_TTL_S", 60),
            stepup_pending_ttl_s: env_int("BSS_PORTAL_STEPUP_PENDING_TTL_S", 10 * 60),
            session_ttl_s: env_int("BSS_PORTAL_SESSION_TTL_S", 24 * 60 * 60),
            login_per_email_max: env_int("BSS_PORTAL_LOGIN_PER_EMAIL_MAX", 3),
            login_per_email_window_s: env_int("BSS_PORTAL_LOGIN_PER_EMAIL_WINDOW_S", 15 * 60),
            login_per_ip_max: env_int("BSS_PORTAL_LOGIN_PER_IP_MAX", 10),
            login_per_ip_window_s: env_int("BSS_PORTAL_LOGIN_PER_IP_WINDOW_S", 60 * 60),
            verify_per_email_max: env_int("BSS_PORTAL_VERIFY_PER_EMAIL_MAX", 10),
            verify_per_email_window_s: env_int("BSS_PORTAL_VERIFY_PER_EMAIL_WINDOW_S", 15 * 60),
            stepup_per_session_max: env_int("BSS_PORTAL_STEPUP_PER_SESSION_MAX", 5),
            stepup_per_session_window_s: env_int("BSS_PORTAL_STEPUP_PER_SESSION_WINDOW_S", 15 * 60),
        }
    }
}

impl Default for Settings {
    fn default() -> Self {
        Self::from_env()
    }
}
