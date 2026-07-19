//! Route gating helpers — the imperative equivalents of the FastAPI
//! `requires_*` dependencies in `bss_self_serve.security`.
//!
//! Each returns `Ok(value)` when the gate passes, or `Err(Response)` carrying a
//! 303 redirect to `/auth/login?next=…` (the port of `RedirectToLogin`). The
//! session/identity come from the [`PortalSession`] extension the middleware
//! attached; the cookie is never touched here.

use axum::response::{IntoResponse, Redirect, Response};

use bss_portal_auth::{IdentityView, SessionView};

use crate::middleware::PortalSession;

/// Minimal query-component percent-encoding for the `next=` parameter.
fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// 303 to `/auth/login`, carrying `next` when non-empty. Port of `RedirectToLogin`.
fn redirect_to_login(next_path: &str) -> Response {
    if next_path.is_empty() {
        Redirect::to("/auth/login").into_response()
    } else {
        Redirect::to(&format!("/auth/login?next={}", urlencode(next_path))).into_response()
    }
}

/// Gate: session present AND `identity.email_verified_at` set. Returns the
/// verified [`IdentityView`]. Port of `requires_verified_email`.
// The `Err` is an axum `Response` (the login redirect) by design — that is the
// value the caller returns straight through, so boxing it would only add churn.
#[allow(clippy::result_large_err)]
pub fn require_verified_email(
    portal: &PortalSession,
    next_path: &str,
) -> Result<IdentityView, Response> {
    match &portal.identity {
        Some(id) if id.email_verified_at.is_some() => Ok(id.clone()),
        _ => Err(redirect_to_login(next_path)),
    }
}

/// Gate: a session is present (not necessarily email-verified). Returns the
/// [`SessionView`]. Port of `requires_session` — used by the step-up routes.
#[allow(clippy::result_large_err)]
pub fn require_session(portal: &PortalSession, next_path: &str) -> Result<SessionView, Response> {
    match &portal.session {
        Some(s) => Ok(s.clone()),
        None => Err(redirect_to_login(next_path)),
    }
}

/// Gate: session + verified email + `identity.customer_id` set. Returns the
/// customer id. Verified-but-unlinked identities bounce to `/` (empty
/// dashboard), not `/plans`. Port of `requires_linked_customer`.
#[allow(clippy::result_large_err)]
pub fn require_linked_customer(
    portal: &PortalSession,
    next_path: &str,
) -> Result<String, Response> {
    let identity = require_verified_email(portal, next_path)?;
    match identity.customer_id {
        Some(cid) if !cid.is_empty() => Ok(cid),
        _ => Err(redirect_to_login("/")),
    }
}
