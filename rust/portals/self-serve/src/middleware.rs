//! `PortalSessionMiddleware` as a tower/axum layer. Port of
//! `bss_self_serve.middleware.session`.
//!
//! Resolves the `bss_portal_session` cookie → `(session, identity)` via
//! [`bss_portal_auth::current_session`], attaches a [`PortalSession`] request
//! extension (`None`s on miss), and — past TTL/2 — rotates the session and
//! writes the new id back as a `Set-Cookie`. This is the ONLY place that reads
//! the session cookie or sets it; route handlers read the extension.

use axum::extract::{Request, State};
use axum::http::header::{HeaderValue, COOKIE, SET_COOKIE};
use axum::middleware::Next;
use axum::response::Response;

use bss_portal_auth::types::{IdentityView, SessionView};

use crate::AppState;

pub const PORTAL_SESSION_COOKIE: &str = "bss_portal_session";

/// Per-request resolved session state, attached as an extension.
#[derive(Clone, Default)]
pub struct PortalSession {
    pub session: Option<SessionView>,
    pub identity: Option<IdentityView>,
    pub customer_id: Option<String>,
}

impl PortalSession {
    /// The verified identity's email, if any (drives the header nav + the
    /// template `request.state.identity`).
    pub fn identity_email(&self) -> Option<&str> {
        self.identity.as_ref().map(|i| i.email.as_str())
    }
}

/// Read a named cookie value from the `Cookie` header.
fn read_cookie(req: &Request, name: &str) -> Option<String> {
    let header = req.headers().get(COOKIE)?.to_str().ok()?;
    for pair in header.split(';') {
        let pair = pair.trim();
        if let Some((k, v)) = pair.split_once('=') {
            if k.trim() == name {
                return Some(v.trim().to_string());
            }
        }
    }
    None
}

/// The session middleware. Wired via `from_fn_with_state(state, session_layer)`.
pub async fn session_layer(
    State(state): State<AppState>,
    mut req: Request,
    next: Next,
) -> Response {
    let mut portal = PortalSession::default();
    let mut rotated_id: Option<String> = None;

    let cookie = read_cookie(&req, PORTAL_SESSION_COOKIE);
    if let (Some(cookie), Some(pool)) = (cookie, &state.db) {
        match bss_portal_auth::current_session(pool, &cookie).await {
            Ok(Some((sess, identity))) => {
                portal.customer_id = identity.customer_id.clone();
                portal.identity = Some(identity);
                // Sliding rotation past TTL/2.
                match bss_portal_auth::rotate_if_due(pool, &sess.id).await {
                    Ok(Some(rotated)) => {
                        rotated_id = Some(rotated.id.clone());
                        portal.session = Some(rotated);
                    }
                    Ok(None) => portal.session = Some(sess),
                    Err(e) => {
                        tracing::warn!(error = %e, "portal_auth.rotate_failed");
                        portal.session = Some(sess);
                    }
                }
            }
            Ok(None) => {}
            Err(e) => tracing::warn!(error = %e, "portal_auth.current_session_failed"),
        }
    }

    req.extensions_mut().insert(portal);
    let mut resp = next.run(req).await;

    if let Some(new_id) = rotated_id {
        if let Ok(value) = HeaderValue::from_str(&build_session_cookie(&new_id, None)) {
            resp.headers_mut().append(SET_COOKIE, value);
        }
    }
    resp
}

/// Compose the `Set-Cookie` header value for the portal session (HttpOnly,
/// SameSite=Lax, Path=/, Secure unless dev-insecure, Max-Age=session TTL).
pub fn build_session_cookie(session_id: &str, max_age: Option<i64>) -> String {
    let settings = bss_portal_auth::Settings::from_env();
    let max_age = max_age.unwrap_or(settings.session_ttl_s);
    let mut parts = vec![
        format!("{PORTAL_SESSION_COOKIE}={session_id}"),
        "Path=/".to_string(),
        "HttpOnly".to_string(),
        "SameSite=Lax".to_string(),
    ];
    if settings.dev_insecure_cookie == 0 {
        parts.push("Secure".to_string());
    }
    parts.push(format!("Max-Age={max_age}"));
    parts.join("; ")
}

/// `Set-Cookie` value that clears the portal session (logout).
pub fn build_clear_cookie() -> String {
    let settings = bss_portal_auth::Settings::from_env();
    let mut parts = vec![
        format!("{PORTAL_SESSION_COOKIE}="),
        "Path=/".to_string(),
        "HttpOnly".to_string(),
        "SameSite=Lax".to_string(),
        "Max-Age=0".to_string(),
        "Expires=Thu, 01 Jan 1970 00:00:00 GMT".to_string(),
    ];
    if settings.dev_insecure_cookie == 0 {
        parts.insert(2, "Secure".to_string());
    }
    parts.join("; ")
}

#[cfg(test)]
mod tests {
    use super::*;

    // Note: the `Secure` flag depends on `BSS_PORTAL_DEV_INSECURE_COOKIE`, so we
    // assert only the env-independent structure (mutating process env here would
    // race sibling tests reading `Settings::from_env`).

    #[test]
    fn session_cookie_shape() {
        let c = build_session_cookie("SES-123", Some(600));
        assert!(c.starts_with("bss_portal_session=SES-123"));
        assert!(c.contains("Path=/"));
        assert!(c.contains("HttpOnly"));
        assert!(c.contains("SameSite=Lax"));
        assert!(c.contains("Max-Age=600"));
    }

    #[test]
    fn clear_cookie_shape() {
        let c = build_clear_cookie();
        assert!(c.starts_with("bss_portal_session="));
        assert!(c.contains("Max-Age=0"));
        assert!(c.contains("Expires=Thu, 01 Jan 1970"));
    }
}
