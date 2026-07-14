//! Email adapters. Port of the adapter surface of `bss_portal_auth.email`
//! (the login-flow parts). `LoggingEmailAdapter` writes a greppable dev mailbox
//! (the hero scenarios `tail` it for the OTP); `NoopEmailAdapter` keeps codes in
//! memory for tests. Resend/SMTP are deferred (fail-fast) until the P6 prod path.
//!
//! Doctrine: the OTP / magic-link plaintext is written ONLY to the mailbox /
//! memory here, never to structlog (the structured log carries length/domain
//! only).

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;

/// The delivery surface the login flow calls.
pub trait EmailAdapter: Send + Sync {
    fn send_login(&self, email: &str, otp: &str, magic_link: &str);
    fn send_step_up(&self, email: &str, otp: &str, action_label: &str);
}

/// Append-only dev mailbox — one block per "send", plain text, greppable.
pub struct LoggingEmailAdapter {
    path: PathBuf,
}

impl LoggingEmailAdapter {
    pub fn new(mailbox_path: impl Into<PathBuf>) -> Self {
        let path = mailbox_path.into();
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        Self { path }
    }

    fn append(&self, lines: &[String]) {
        use std::io::Write;
        let ts = bss_clock::isoformat(bss_clock::now());
        if let Ok(mut fh) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
        {
            let mut buf = format!("\n=== {ts} ===\n");
            for line in lines {
                buf.push_str(line);
                buf.push('\n');
            }
            let _ = fh.write_all(buf.as_bytes());
        }
    }
}

fn email_domain(email: &str) -> String {
    email
        .split_once('@')
        .map(|(_, d)| d.to_string())
        .unwrap_or_else(|| "?".to_string())
}

impl EmailAdapter for LoggingEmailAdapter {
    fn send_login(&self, email: &str, otp: &str, magic_link: &str) {
        // e2e contract: helpers match on the "portal login code" substring —
        // keep it invariant however the brand is set.
        let brand_name = bss_branding::current(None).brand_name;
        self.append(&[
            format!("To: {email}"),
            format!("Subject: Your {brand_name} portal login code"),
            String::new(),
            format!("OTP: {otp}"),
            format!("Magic link: {magic_link}"),
            String::new(),
            "Code expires in 15 minutes.".to_string(),
        ]);
        tracing::info!(
            adapter = "logging",
            email_domain = %email_domain(email),
            "portal_auth.email.login_sent",
        );
    }

    fn send_step_up(&self, email: &str, otp: &str, action_label: &str) {
        self.append(&[
            format!("To: {email}"),
            format!("Subject: Confirm action: {action_label}"),
            String::new(),
            format!("OTP: {otp}"),
            format!("Action: {action_label}"),
            String::new(),
            "Code expires in 5 minutes.".to_string(),
        ]);
        tracing::info!(
            adapter = "logging",
            action = action_label,
            "portal_auth.email.step_up_sent"
        );
    }
}

/// In-memory test adapter — keeps the most recent code per `(email, kind)`.
#[derive(Default)]
pub struct NoopEmailAdapter {
    records: Mutex<HashMap<(String, String), (String, String)>>,
}

impl NoopEmailAdapter {
    pub fn new() -> Self {
        Self::default()
    }

    /// `(otp, magic_link)` for the most recent login send to `email`.
    pub fn last_login_codes(&self, email: &str) -> Option<(String, String)> {
        #[allow(clippy::unwrap_used)]
        let r = self.records.lock().unwrap();
        r.get(&(email.to_string(), "login".to_string())).cloned()
    }
}

impl EmailAdapter for NoopEmailAdapter {
    fn send_login(&self, email: &str, otp: &str, magic_link: &str) {
        #[allow(clippy::unwrap_used)]
        let mut r = self.records.lock().unwrap();
        r.insert(
            (email.to_string(), "login".to_string()),
            (otp.to_string(), magic_link.to_string()),
        );
    }
    fn send_step_up(&self, email: &str, otp: &str, _action_label: &str) {
        #[allow(clippy::unwrap_used)]
        let mut r = self.records.lock().unwrap();
        r.insert(
            (email.to_string(), "step_up".to_string()),
            (otp.to_string(), String::new()),
        );
    }
}

/// Reconcile the new `BSS_PORTAL_EMAIL_PROVIDER` with the legacy
/// `BSS_PORTAL_EMAIL_ADAPTER`: both empty → `"logging"`; only legacy → legacy;
/// otherwise the new value.
pub fn resolve_provider_name(provider: &str, legacy_adapter: &str) -> String {
    let new = provider.trim();
    let old = legacy_adapter.trim();
    if new.is_empty() && old.is_empty() {
        return "logging".to_string();
    }
    if new.is_empty() {
        tracing::warn!(
            "BSS_PORTAL_EMAIL_ADAPTER is deprecated; rename to BSS_PORTAL_EMAIL_PROVIDER"
        );
        return old.to_string();
    }
    new.to_string()
}

/// Resolve a provider name → a concrete adapter. Unknown / not-yet-ported
/// providers fail fast (never silently downgrade to no-op delivery).
pub fn select_adapter(
    name: &str,
    mailbox_path: &str,
    resend_api_key: &str,
    from_address: &str,
) -> Result<Box<dyn EmailAdapter>, String> {
    match name.to_lowercase().as_str() {
        "logging" => Ok(Box::new(LoggingEmailAdapter::new(mailbox_path))),
        "noop" => Ok(Box::new(NoopEmailAdapter::new())),
        "resend" => {
            // The Resend HTML adapter is not yet ported (P6b) — fail fast so a
            // prod misconfig is loud rather than silently dropping mail.
            if resend_api_key.is_empty() || from_address.is_empty() {
                return Err("resend requires BSS_PORTAL_EMAIL_RESEND_API_KEY + \
                            BSS_PORTAL_EMAIL_FROM"
                    .to_string());
            }
            Err(
                "resend email adapter not yet ported in the Rust portal (P6b); \
                 use logging in dev"
                    .to_string(),
            )
        }
        "smtp" => Err("smtp email adapter is reserved (post-v0.16)".to_string()),
        other => Err(format!(
            "Unknown BSS_PORTAL_EMAIL_PROVIDER={other:?}; expected 'logging', \
             'noop', 'resend', or 'smtp'"
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_provider() {
        assert_eq!(resolve_provider_name("", ""), "logging");
        assert_eq!(resolve_provider_name("noop", ""), "noop");
        assert_eq!(resolve_provider_name("", "resend"), "resend");
        assert_eq!(resolve_provider_name("logging", "resend"), "logging");
    }

    #[test]
    fn noop_keeps_last_codes() {
        let a = NoopEmailAdapter::new();
        a.send_login("x@y.com", "123456", "http://link");
        assert_eq!(
            a.last_login_codes("x@y.com"),
            Some(("123456".to_string(), "http://link".to_string()))
        );
        assert!(a.last_login_codes("other@y.com").is_none());
    }

    #[test]
    fn select_logging_and_noop_ok_resend_fails() {
        assert!(select_adapter("logging", "/tmp/mb.log", "", "").is_ok());
        assert!(select_adapter("noop", "", "", "").is_ok());
        assert!(select_adapter("resend", "", "", "").is_err());
        assert!(select_adapter("bogus", "", "", "").is_err());
    }
}
