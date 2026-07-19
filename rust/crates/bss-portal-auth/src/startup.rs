//! Fail-fast startup validator for `BSS_PORTAL_TOKEN_PEPPER`. Port of
//! `bss_portal_auth.startup`.
//!
//! Called from the portal's lifespan BEFORE any auth flow can run. If the pepper
//! is unset, still the sentinel, or shorter than 32 chars, the portal refuses to
//! start. Mirrors the bss-middleware `validate_api_token_present` pattern.
//! Pepper rotation invalidates every in-flight login code, so this is
//! deliberately startup-only — hot-reload is not supported.

use crate::config::Settings;

const SENTINEL: &str = "changeme";
const MIN_LENGTH: usize = 32;

/// `Err(message)` if the pepper is unset / sentinel / short; `Ok(())` otherwise.
/// The message strings match the Python `RuntimeError` copy.
pub fn validate_pepper_present() -> Result<(), String> {
    let pepper = Settings::from_env().token_pepper;
    if pepper.is_empty() {
        return Err(
            "BSS_PORTAL_TOKEN_PEPPER is unset; set it in .env before starting the \
             portal. Generate via: openssl rand -hex 32"
                .to_string(),
        );
    }
    if pepper == SENTINEL {
        return Err(format!(
            "BSS_PORTAL_TOKEN_PEPPER is still the .env.example sentinel ({SENTINEL:?}); \
             replace with a real value. Generate via: openssl rand -hex 32"
        ));
    }
    if pepper.len() < MIN_LENGTH {
        return Err(format!(
            "BSS_PORTAL_TOKEN_PEPPER too short ({} chars; need >={MIN_LENGTH}). \
             Generate via: openssl rand -hex 32",
            pepper.len()
        ));
    }
    tracing::info!(length = pepper.len(), "portal_auth.pepper.validated");
    Ok(())
}
