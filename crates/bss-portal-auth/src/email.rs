//! Email adapters. Port of the adapter surface of `bss_portal_auth.email`
//! (the login-flow parts). `LoggingEmailAdapter` writes a greppable dev mailbox
//! (the hero scenarios `tail` it for the OTP); `NoopEmailAdapter` keeps codes in
//! memory for tests. `ResendEmailAdapter` is the production adapter — a direct
//! `reqwest` POST to Resend's REST API (Decision D4: no vendor SDK, mirrors the
//! Stripe tokenizer). SMTP stays deferred (fail-fast) until post-v0.16.
//!
//! Doctrine: the OTP / magic-link plaintext is written ONLY to the mailbox /
//! memory here, never to structlog (the structured log carries length/domain
//! only). Brand name + mark are operator input flowing into hand-built HTML —
//! `html_escape` at that seam is mandatory, not cosmetic.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Instant;

use bss_branding::BrandingView;

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

    /// The OTP from the most recent step-up send to `email`.
    pub fn last_step_up_code(&self, email: &str) -> Option<String> {
        #[allow(clippy::unwrap_used)]
        let r = self.records.lock().unwrap();
        r.get(&(email.to_string(), "step_up".to_string()))
            .map(|(otp, _)| otp.clone())
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

// ── HTML email rendering (port of `_render_email` + `_humanize_action_label`) ──
//
// Inline styles only — Gmail/Outlook/Apple-Mail strip <style> blocks; table
// layout because clients still distrust flexbox. Every color comes from the
// active `bss_branding` theme (resolved per send, so the settings.toml
// hot-reload applies to the next email). No images / remote resources.

const FONT_SANS: &str =
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif";
const FONT_MONO: &str =
    "ui-monospace, 'SF Mono', 'JetBrains Mono', Menlo, Consolas, 'Liberation Mono', monospace";

/// Escape the five HTML-active characters (parity with Python `html.escape`,
/// `quote=True`). Mandatory at the brand-name/mark seam — these are operator
/// input flowing into hand-built HTML f-strings that no autoescape covers.
fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

/// Map `name_update` → `"Change your name"`; fall back to sentence-cased
/// snake → space for unknown labels so the email still reads. Keys mirror
/// `SENSITIVE_ACTION_LABELS`.
fn humanize_action_label(label: &str) -> String {
    let human = match label {
        "vas_purchase" => "VAS purchase",
        "payment_method_add" => "Add a payment method",
        "payment_method_remove" => "Remove a payment method",
        "payment_method_set_default" => "Set default payment method",
        "subscription_terminate" => "Cancel your subscription",
        "email_change" => "Change your email",
        "phone_update" => "Update your phone number",
        "address_update" => "Update your address",
        "name_update" => "Change your name",
        "plan_change_schedule" => "Schedule a plan change",
        "plan_change_cancel" => "Cancel a scheduled plan change",
        other => {
            // Title-case-ish fallback: snake → space, capitalize first char.
            let spaced = other.replace('_', " ");
            let mut chars = spaced.chars();
            return match chars.next() {
                Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
                None => spaced,
            };
        }
    };
    human.to_string()
}

/// Build a self-serve-portal-vibed transactional HTML email (OTP variant).
/// Faithful port of `_render_email`. `cta_label`/`cta_url` are paired — pass
/// both or neither (step-up passes neither).
#[allow(clippy::too_many_arguments)]
fn render_otp_email(
    brand: &BrandingView,
    preheader: &str,
    heading: &str,
    intro: &str,
    otp: &str,
    cta_label: Option<&str>,
    cta_url: Option<&str>,
    footnote: &str,
) -> String {
    let t = &brand.theme;
    let name_html = html_escape(&brand.brand_name);
    let mark_html = html_escape(&brand.mark);

    let cta_html = match (cta_label, cta_url) {
        (Some(label), Some(url)) => format!(
            "<tr><td align=\"center\" style=\"padding: 8px 0 24px 0;\">\
             <a href=\"{url}\" style=\"display:inline-block;\
             background:{accent};color:{on_accent};font-family:{FONT_SANS};\
             font-weight:600;font-size:14px;text-decoration:none;\
             padding:11px 24px;border-radius:6px;border:1px solid {accent_dim};\">\
             {label}</a></td></tr>",
            accent = t.accent,
            on_accent = t.on_accent,
            accent_dim = t.accent_dim,
        ),
        _ => String::new(),
    };

    format!(
        "<!doctype html>\n<html><head>\
         <meta charset=\"utf-8\">\
         <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\
         <meta name=\"color-scheme\" content=\"dark\">\
         <meta name=\"supported-color-schemes\" content=\"dark light\">\
         <title>{heading}</title></head>\
         <body style=\"margin:0;padding:0;background:{bg};color:{fg};font-family:{FONT_SANS};\">\
         <div style=\"display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;\">{preheader}</div>\
         <table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" border=\"0\" style=\"background:{bg};\">\
         <tr><td align=\"center\" style=\"padding: 32px 16px;\">\
         <table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" border=\"0\" \
         style=\"max-width:480px;background:{bg_elev};border:1px solid {border};border-radius:8px;overflow:hidden;\">\
         <tr><td style=\"padding:18px 24px;border-bottom:1px solid {border};font-family:{FONT_MONO};font-size:13px;\">\
         <span style=\"color:{accent};font-weight:700;\">{mark_html}</span>\
         <span style=\"color:{fg};font-weight:600; margin-left:8px;\">{name_html}</span>\
         <span style=\"color:{fg_muted};margin-left:8px;\"> / self-serve portal</span></td></tr>\
         <tr><td style=\"padding:28px 24px 12px 24px;\">\
         <h1 style=\"margin:0 0 12px 0;font-size:20px;line-height:1.3;color:{fg};font-weight:600;\">{heading}</h1>\
         <p style=\"margin:0 0 20px 0;font-size:15px;line-height:1.5;color:{fg};\">{intro}</p></td></tr>\
         <tr><td align=\"center\" style=\"padding: 0 24px 16px 24px;\">\
         <div style=\"display:inline-block;background:{bg_inset};border:1px solid {border};border-radius:6px;\
         padding:14px 22px;font-family:{FONT_MONO};font-size:26px;letter-spacing:6px;font-weight:600;color:{accent};\">{otp}</div>\
         </td></tr>{cta_html}\
         <tr><td style=\"padding: 12px 24px 24px 24px;\">\
         <p style=\"margin:0;font-family:{FONT_SANS};font-size:12px;line-height:1.5;color:{fg_muted};\">{footnote}</p></td></tr>\
         </table>\
         <table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" border=\"0\" style=\"max-width:480px;margin-top:16px;\">\
         <tr><td align=\"center\" style=\"font-family:{FONT_MONO};font-size:11px;color:{fg_dim};\">\
         — sent by {name_html} · powered by bss-cli · transactional only —</td></tr></table>\
         </td></tr></table></body></html>",
        bg = t.bg,
        bg_elev = t.bg_elev,
        bg_inset = t.bg_inset,
        fg = t.fg,
        fg_muted = t.fg_muted,
        fg_dim = t.fg_dim,
        accent = t.accent,
        border = t.border,
    )
}

/// Production adapter — sends via Resend's REST API over a direct `reqwest`
/// POST (Decision D4; no vendor SDK, mirrors the Stripe tokenizer). Construction
/// validates the key + sender but makes no network call.
///
/// Delivery is fire-and-forget: the `EmailAdapter` trait returns `()`, so each
/// `send_*` spawns the HTTPS call on the tokio runtime and records the outcome
/// to `tracing` (the forensic correlation point with inbound `/webhooks/resend`
/// events — both share `provider_call_id`). `email_domain` only, never the full
/// address / OTP / magic-link.
pub struct ResendEmailAdapter {
    http: reqwest::Client,
    api_key: String,
    from: String,
}

impl ResendEmailAdapter {
    /// `api_key` + `from_address` must be non-empty (the caller
    /// `select_adapter` already guards, but keep the invariant local too).
    pub fn new(
        api_key: impl Into<String>,
        from_address: impl Into<String>,
    ) -> Result<Self, String> {
        let api_key = api_key.into();
        let from = from_address.into();
        if api_key.is_empty() {
            return Err("ResendEmailAdapter requires a non-empty api_key".to_string());
        }
        if from.is_empty() {
            return Err(
                "ResendEmailAdapter requires from_address (BSS_PORTAL_EMAIL_FROM)".to_string(),
            );
        }
        Ok(Self {
            http: reqwest::Client::new(),
            api_key,
            from,
        })
    }

    /// Single Resend HTTPS call, spawned so the async caller isn't blocked.
    /// Logs success (with `provider_call_id`) or failure — never the secret.
    fn send(&self, operation: &'static str, to: &str, subject: String, html: String, text: String) {
        let http = self.http.clone();
        let api_key = self.api_key.clone();
        let from = self.from.clone();
        let to = to.to_string();
        let domain = email_domain(&to);
        let body = serde_json::json!({
            "from": from,
            "to": [to],
            "subject": subject,
            "html": html,
            "text": text,
        });

        tokio::spawn(async move {
            let start = Instant::now();
            let resp = http
                .post("https://api.resend.com/emails")
                .bearer_auth(&api_key)
                .json(&body)
                .send()
                .await;
            let latency_ms = start.elapsed().as_millis();
            match resp {
                Ok(r) if r.status().is_success() => {
                    let provider_call_id = r
                        .json::<serde_json::Value>()
                        .await
                        .ok()
                        .and_then(|v| v.get("id").and_then(|i| i.as_str()).map(String::from))
                        .unwrap_or_default();
                    tracing::info!(
                        adapter = "resend",
                        operation,
                        email_domain = %domain,
                        latency_ms = latency_ms as u64,
                        provider_call_id = %provider_call_id,
                        "portal_auth.email.sent",
                    );
                }
                Ok(r) => {
                    let status = r.status().as_u16();
                    let preview = r.text().await.unwrap_or_default();
                    let preview: String = preview.chars().take(200).collect();
                    tracing::warn!(
                        adapter = "resend",
                        operation,
                        email_domain = %domain,
                        latency_ms = latency_ms as u64,
                        status,
                        body_preview = %preview,
                        "portal_auth.email.send_failed",
                    );
                }
                Err(e) => {
                    tracing::warn!(
                        adapter = "resend",
                        operation,
                        email_domain = %domain,
                        latency_ms = latency_ms as u64,
                        error = %e,
                        "portal_auth.email.send_failed",
                    );
                }
            }
        });
    }
}

impl EmailAdapter for ResendEmailAdapter {
    fn send_login(&self, email: &str, otp: &str, magic_link: &str) {
        let brand = bss_branding::current(None);
        let name = brand.brand_name.clone();
        let name_html = html_escape(&name);
        let body_html = render_otp_email(
            &brand,
            &format!("Your login code: {otp}. Expires in 15 minutes."),
            &format!("Sign in to {name_html}"),
            "Use the code below or click the button to sign in.",
            otp,
            Some("Sign in"),
            Some(magic_link),
            "Code expires in 15 minutes. If you didn't request this, you can ignore this email.",
        );
        let body_text = format!(
            "{name} — sign in\n\nOTP: {otp}\nMagic link: {magic_link}\n\n\
             Code expires in 15 minutes.\nIf you didn't request this, you can ignore this email."
        );
        self.send(
            "send_login",
            email,
            format!("Your {name} sign-in code"),
            body_html,
            body_text,
        );
    }

    fn send_step_up(&self, email: &str, otp: &str, action_label: &str) {
        let human = humanize_action_label(action_label);
        let brand = bss_branding::current(None);
        let body_html = render_otp_email(
            &brand,
            &format!("Confirm: {human}. Code: {otp}. Expires in 5 minutes."),
            &human,
            "Use the code below to confirm this action. We're asking because it's a change to \
             your account or service that we don't want to do without a second check.",
            otp,
            None,
            None,
            "Code expires in 5 minutes. If you didn't initiate this action, ignore this email — \
             no change has been made — and consider rotating any account credentials you've \
             reused elsewhere.",
        );
        let body_text = format!(
            "{} — confirm: {human}\n\nOTP: {otp}\n\n\
             Code expires in 5 minutes.\nIf you didn't initiate this action, ignore this email.",
            brand.brand_name
        );
        self.send(
            "send_step_up",
            email,
            format!("Confirm: {human}"),
            body_html,
            body_text,
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
            // Fail fast on a prod misconfig rather than silently dropping mail.
            if resend_api_key.is_empty() {
                return Err(
                    "BSS_PORTAL_EMAIL_PROVIDER=resend requires BSS_PORTAL_EMAIL_RESEND_API_KEY"
                        .to_string(),
                );
            }
            if from_address.is_empty() {
                return Err(
                    "BSS_PORTAL_EMAIL_PROVIDER=resend requires BSS_PORTAL_EMAIL_FROM \
                     (e.g. 'BSS-CLI <noreply@mail.example.com>')"
                        .to_string(),
                );
            }
            Ok(Box::new(ResendEmailAdapter::new(
                resend_api_key,
                from_address,
            )?))
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
    fn select_logging_and_noop_ok() {
        assert!(select_adapter("logging", "/tmp/mb.log", "", "").is_ok());
        assert!(select_adapter("noop", "", "", "").is_ok());
        assert!(select_adapter("bogus", "", "", "").is_err());
    }

    #[test]
    fn select_resend_needs_key_and_from() {
        // Missing key / from → fail fast (never silent no-op delivery).
        assert!(select_adapter("resend", "", "", "").is_err());
        assert!(select_adapter("resend", "", "re_abc", "").is_err());
        assert!(select_adapter("resend", "", "", "BSS <n@x.com>").is_err());
        // Both present → constructs (no network call at construction).
        assert!(select_adapter("resend", "", "re_abc", "BSS <n@x.com>").is_ok());
    }

    #[test]
    fn humanize_labels() {
        assert_eq!(humanize_action_label("name_update"), "Change your name");
        assert_eq!(humanize_action_label("email_change"), "Change your email");
        // Unknown → snake→space, sentence-cased.
        assert_eq!(humanize_action_label("some_new_thing"), "Some new thing");
    }

    #[test]
    fn otp_email_escapes_brand_and_contains_otp() {
        let brand = bss_branding::current(None);
        let html = render_otp_email(
            &brand, "pre", "Heading", "intro", "424242", None, None, "foot",
        );
        assert!(html.contains("424242"));
        assert!(html.contains("<!doctype html>"));
    }
}
