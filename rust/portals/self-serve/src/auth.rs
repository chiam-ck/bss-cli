//! `/auth/*` routes — email login, check-email/OTP, magic-link verify, logout.
//! Port of the login surface of `bss_self_serve.routes.auth` (step-up deferred).
//!
//! Every route is public (the `/auth/` allowlist prefix) — these routes ARE the
//! gate. DB writes go through `bss_portal_auth` only; email via the selected
//! adapter. The customer-facing failure copy is generic; the internal
//! `LoginFailed.reason` is for the audit log.

use axum::extract::{Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Redirect, Response};
use axum::Form;
use minijinja::context;
use serde::Deserialize;

use bss_portal_auth::{start_email_login, verify_email_login, LoginError, VerifyOutcome};

use crate::middleware::{build_clear_cookie, build_session_cookie, PortalSession};
use crate::routes::render;
use crate::security::safe_next_path;
use crate::templating::request_ctx;
use crate::AppState;

// ── query/form payloads ──────────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct LoginQuery {
    next: Option<String>,
}

#[derive(Deserialize)]
pub struct LoginForm {
    email: String,
    #[serde(default)]
    next: Option<String>,
}

#[derive(Deserialize)]
pub struct CheckEmailQuery {
    email: String,
    next: Option<String>,
}

#[derive(Deserialize)]
pub struct CheckEmailForm {
    email: String,
    code: String,
    #[serde(default)]
    next: Option<String>,
}

#[derive(Deserialize)]
pub struct VerifyQuery {
    email: String,
    token: String,
    next: Option<String>,
}

// ── helpers ──────────────────────────────────────────────────────────────────

fn user_agent(headers: &HeaderMap) -> Option<String> {
    headers
        .get(axum::http::header::USER_AGENT)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string)
}

/// `ada@example.sg` → `a***@example.sg`.
fn mask_email(email: &str) -> String {
    let Some((local, domain)) = email.split_once('@') else {
        return email.to_string();
    };
    if local.is_empty() {
        return email.to_string();
    }
    let head = &local[..1];
    let stars = "*".repeat((local.chars().count() - 1).max(1));
    format!("{head}{stars}@{domain}")
}

/// 303 to `next_path` with the fresh session cookie set (replaces any the
/// middleware queued — it only rotates existing sessions).
fn redirect_with_cookie(next_path: &str, session_id: &str) -> Response {
    (
        [(
            axum::http::header::SET_COOKIE,
            build_session_cookie(session_id, None),
        )],
        Redirect::to(next_path),
    )
        .into_response()
}

// ── /auth/login ──────────────────────────────────────────────────────────────

pub async fn login_form(
    State(state): State<AppState>,
    Extension(portal): axum::extract::Extension<PortalSession>,
    Query(q): Query<LoginQuery>,
) -> Response {
    let next_path = safe_next_path(q.next.as_deref(), "/");
    render(
        &state,
        "auth_login.html",
        context! {
            next_path => next_path,
            error => Option::<String>::None,
            email => "",
            request => request_ctx("/auth/login", portal.identity_email()),
        },
    )
}

pub async fn login_submit(
    State(state): State<AppState>,
    Extension(portal): axum::extract::Extension<PortalSession>,
    headers: HeaderMap,
    Form(form): Form<LoginForm>,
) -> Response {
    let next_path = safe_next_path(form.next.as_deref(), "/");
    let email = form.email.trim().to_lowercase();

    let render_err = |state: &AppState, msg: &str, status: StatusCode| -> Response {
        let mut r = render(
            state,
            "auth_login.html",
            context! {
                next_path => next_path.clone(),
                error => msg,
                email => email.clone(),
                request => request_ctx("/auth/login", portal.identity_email()),
            },
        );
        *r.status_mut() = status;
        r
    };

    // Cheap shape validation (policy stays here; bss-portal-auth treats email as opaque).
    if !email.contains('@') || email.len() < 3 || email.contains(' ') {
        return render_err(
            &state,
            "That doesn't look like an email address.",
            StatusCode::BAD_REQUEST,
        );
    }

    let (Some(pool), Some(adapter)) = (&state.db, &state.email_adapter) else {
        return render_err(
            &state,
            "Login is temporarily unavailable.",
            StatusCode::SERVICE_UNAVAILABLE,
        );
    };

    match start_email_login(
        pool,
        &email,
        None,
        user_agent(&headers).as_deref(),
        adapter.as_ref(),
    )
    .await
    {
        Ok(_) => {
            let qs = format!("email={}&next={}", urlencode(&email), urlencode(&next_path));
            Redirect::to(&format!("/auth/check-email?{qs}")).into_response()
        }
        Err(LoginError::RateLimited(_)) => render_err(
            &state,
            "Too many attempts. Try again in a few minutes.",
            StatusCode::TOO_MANY_REQUESTS,
        ),
        Err(LoginError::Db(e)) => {
            tracing::error!(error = %e, "portal_auth.login.db_error");
            render_err(
                &state,
                "Something went wrong. Try again.",
                StatusCode::INTERNAL_SERVER_ERROR,
            )
        }
    }
}

// ── /auth/check-email ────────────────────────────────────────────────────────

pub async fn check_email_form(
    State(state): State<AppState>,
    Extension(portal): axum::extract::Extension<PortalSession>,
    Query(q): Query<CheckEmailQuery>,
) -> Response {
    let next_path = safe_next_path(q.next.as_deref(), "/");
    render(
        &state,
        "auth_check_email.html",
        context! {
            email => q.email,
            email_masked => mask_email(&q.email),
            next_path => next_path,
            error => Option::<String>::None,
            request => request_ctx("/auth/check-email", portal.identity_email()),
        },
    )
}

pub async fn check_email_submit(
    State(state): State<AppState>,
    Extension(portal): axum::extract::Extension<PortalSession>,
    headers: HeaderMap,
    Form(form): Form<CheckEmailForm>,
) -> Response {
    let next_path = safe_next_path(form.next.as_deref(), "/");
    let email = form.email.trim().to_lowercase();
    let code = form.code.trim().to_string();

    let render_err = |state: &AppState, msg: &str, status: StatusCode| -> Response {
        let mut r = render(
            state,
            "auth_check_email.html",
            context! {
                email => email.clone(),
                email_masked => mask_email(&email),
                next_path => next_path.clone(),
                error => msg,
                request => request_ctx("/auth/check-email", portal.identity_email()),
            },
        );
        *r.status_mut() = status;
        r
    };

    let Some(pool) = &state.db else {
        return render_err(
            &state,
            "Login is temporarily unavailable.",
            StatusCode::SERVICE_UNAVAILABLE,
        );
    };

    match verify_email_login(pool, &email, &code, None, user_agent(&headers).as_deref()).await {
        Ok(VerifyOutcome::Session(sess)) => redirect_with_cookie(&next_path, &sess.id),
        Ok(VerifyOutcome::Failed(f)) => {
            tracing::info!(reason = %f.reason, "portal_auth.login.failed");
            render_err(
                &state,
                "Incorrect or expired code. Try again or request a new one.",
                StatusCode::BAD_REQUEST,
            )
        }
        Err(LoginError::RateLimited(_)) => render_err(
            &state,
            "Too many attempts. Try again in a few minutes.",
            StatusCode::TOO_MANY_REQUESTS,
        ),
        Err(LoginError::Db(e)) => {
            tracing::error!(error = %e, "portal_auth.verify.db_error");
            render_err(
                &state,
                "Something went wrong. Try again.",
                StatusCode::INTERNAL_SERVER_ERROR,
            )
        }
    }
}

// ── /auth/verify (magic-link landing) ────────────────────────────────────────

pub async fn verify_magic_link(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(q): Query<VerifyQuery>,
) -> Response {
    let next_path = safe_next_path(q.next.as_deref(), "/");
    let email = q.email.trim().to_lowercase();

    let Some(pool) = &state.db else {
        return Redirect::to(&format!("/auth/login?next={}", urlencode(&next_path)))
            .into_response();
    };

    match verify_email_login(
        pool,
        &email,
        &q.token,
        None,
        user_agent(&headers).as_deref(),
    )
    .await
    {
        Ok(VerifyOutcome::Session(sess)) => redirect_with_cookie(&next_path, &sess.id),
        _ => Redirect::to(&format!("/auth/login?next={}", urlencode(&next_path))).into_response(),
    }
}

// ── /auth/logout ─────────────────────────────────────────────────────────────

pub async fn logout(
    State(state): State<AppState>,
    Extension(portal): axum::extract::Extension<PortalSession>,
) -> Response {
    if let (Some(pool), Some(sess)) = (&state.db, &portal.session) {
        if let Err(e) = bss_portal_auth::revoke_session(pool, &sess.id).await {
            tracing::warn!(error = %e, "portal_auth.logout.revoke_failed");
        }
    }
    (
        [(axum::http::header::SET_COOKIE, build_clear_cookie())],
        Redirect::to("/welcome"),
    )
        .into_response()
}

/// Minimal query-component percent-encoding (`urlencode` for `email`/`next`).
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

use axum::extract::Extension;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn masks_email() {
        // `'*' * max(len(local)-1, 1)` — "ada" (3) → 2 stars (the Python
        // docstring's "a***" is wrong; the code produces "a**").
        assert_eq!(mask_email("ada@example.sg"), "a**@example.sg");
        assert_eq!(mask_email("x@y.com"), "x*@y.com");
        assert_eq!(mask_email("noat"), "noat");
    }
}
