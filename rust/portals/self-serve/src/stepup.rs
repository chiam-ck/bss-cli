//! `/auth/step-up` routes + the `check_step_up` gate. Port of the step-up surface
//! of `bss_self_serve.routes.auth` + `security.requires_step_up`.
//!
//! A sensitive write (`SENSITIVE_ACTION_LABELS`) needs a one-shot step-up grant.
//! The gate reads the grant from (in order) the `X-BSS-StepUp-Token` header, the
//! `step_up_token` form field, or the `bss_portal_step_up` cookie; if absent /
//! invalid it stashes the POST body and bounces to `/auth/step-up`. After OTP
//! verify, the grant lands in a short-lived cookie and the stashed body is
//! replayed (auto-submitting form) so the customer never re-types.

use axum::extract::{Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Redirect, Response};
use axum::{Extension, Form};
use minijinja::context;
use serde::Deserialize;

use bss_portal_auth::{
    consume_pending_action, consume_step_up_token, start_step_up, stash_pending_action,
    verify_step_up, StepUpError, StepUpVerify,
};

use crate::deps::require_session;
use crate::middleware::PortalSession;
use crate::routes::render;
use crate::security::{safe_next_path, SENSITIVE_ACTION_LABELS};
use crate::templating::request_ctx;
use crate::AppState;

const GRANT_COOKIE: &str = "bss_portal_step_up";

// ── query/form payloads ──────────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct StepUpFormQuery {
    action: String,
    next: Option<String>,
}

#[derive(Deserialize)]
pub struct StepUpStartForm {
    action: String,
    #[serde(default = "root")]
    next: String,
}

#[derive(Deserialize)]
pub struct StepUpVerifyForm {
    code: String,
    action: String,
    #[serde(default = "root")]
    next: String,
}

fn root() -> String {
    "/".to_string()
}

// ── GET /auth/step-up ────────────────────────────────────────────────────────

pub async fn step_up_form(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Query(q): Query<StepUpFormQuery>,
) -> Response {
    let next_path = safe_next_path(q.next.as_deref(), "/");
    render_form(
        &state,
        &portal,
        &q.action,
        &next_path,
        false,
        None,
        StatusCode::OK,
    )
}

// ── POST /auth/step-up/start ─────────────────────────────────────────────────

pub async fn step_up_start(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    headers: HeaderMap,
    Form(form): Form<StepUpStartForm>,
) -> Response {
    let next_path = safe_next_path(Some(&form.next), "/");
    let sess = match require_session(&portal, &next_path) {
        Ok(s) => s,
        Err(r) => return r,
    };
    let (Some(pool), Some(adapter)) = (&state.db, &state.email_adapter) else {
        return render_form(
            &state,
            &portal,
            &form.action,
            &next_path,
            false,
            Some("Step-up is temporarily unavailable."),
            StatusCode::SERVICE_UNAVAILABLE,
        );
    };

    match start_step_up(
        pool,
        &sess.id,
        &form.action,
        None,
        user_agent(&headers).as_deref(),
        adapter.as_ref(),
    )
    .await
    {
        Ok(_) => render_form(
            &state,
            &portal,
            &form.action,
            &next_path,
            true,
            None,
            StatusCode::OK,
        ),
        Err(StepUpError::RateLimited(_)) => render_form(
            &state,
            &portal,
            &form.action,
            &next_path,
            false,
            Some("Too many attempts. Try again later."),
            StatusCode::TOO_MANY_REQUESTS,
        ),
        Err(e) => {
            tracing::error!(error = %e, "portal_auth.step_up.start_error");
            render_form(
                &state,
                &portal,
                &form.action,
                &next_path,
                false,
                Some("Something went wrong. Try again."),
                StatusCode::INTERNAL_SERVER_ERROR,
            )
        }
    }
}

// ── POST /auth/step-up (verify) ──────────────────────────────────────────────

pub async fn step_up_verify(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Form(form): Form<StepUpVerifyForm>,
) -> Response {
    let next_path = safe_next_path(Some(&form.next), "/");
    let sess = match require_session(&portal, &next_path) {
        Ok(s) => s,
        Err(r) => return r,
    };
    let Some(pool) = &state.db else {
        return render_form(
            &state,
            &portal,
            &form.action,
            &next_path,
            true,
            Some("Step-up is temporarily unavailable."),
            StatusCode::SERVICE_UNAVAILABLE,
        );
    };

    let result = match verify_step_up(pool, &sess.id, form.code.trim(), &form.action).await {
        Ok(r) => r,
        Err(e) => {
            tracing::error!(error = %e, "portal_auth.step_up.verify_error");
            return render_form(
                &state,
                &portal,
                &form.action,
                &next_path,
                true,
                Some("Something went wrong. Try again."),
                StatusCode::INTERNAL_SERVER_ERROR,
            );
        }
    };

    let grant = match result {
        StepUpVerify::Token(t) => t.token,
        StepUpVerify::Failed(f) => {
            tracing::info!(reason = %f.reason, action = %form.action, "portal_auth.step_up.failed");
            return render_form(
                &state,
                &portal,
                &form.action,
                &next_path,
                true,
                Some("Incorrect or expired code. Request a new one above."),
                StatusCode::BAD_REQUEST,
            );
        }
    };

    // Replay the stashed POST body if one is in flight; else 303 to `next`.
    let pending = consume_pending_action(pool, &sess.id, &form.action)
        .await
        .unwrap_or(None);

    let mut resp = if let Some(p) = pending {
        let mut payload = serde_json::Map::new();
        for (k, v) in &p.payload {
            payload.insert(k.clone(), serde_json::Value::String(v.clone()));
        }
        render(
            &state,
            "auth_step_up_replay.html",
            context! {
                target_url => p.target_url,
                payload => minijinja::Value::from_serialize(&payload),
                action_label => form.action,
                request => request_ctx("/auth/step-up", portal.identity_email()),
            },
        )
    } else {
        Redirect::to(&next_path).into_response()
    };
    if let Ok(v) = axum::http::HeaderValue::from_str(&build_grant_cookie(&grant)) {
        resp.headers_mut().append(axum::http::header::SET_COOKIE, v);
    }
    resp
}

// ── the gate used by sensitive routes ────────────────────────────────────────

/// Consume a one-shot step-up grant for `action_label`. `Ok(())` when a valid
/// grant is present; `Err(Response)` is the 303 bounce to `/auth/step-up` (with
/// the POST body stashed for replay). Port of `requires_step_up`.
///
/// `form` is the raw POST fields (for the `step_up_token` field + the stash);
/// `target` is the request path+query (the replay POST target). The GET bounce
/// lands on the safe same-origin Referer path when present, else `target`.
#[allow(clippy::result_large_err)]
pub async fn check_step_up(
    state: &AppState,
    portal: &PortalSession,
    action_label: &str,
    headers: &HeaderMap,
    form: &[(String, String)],
    target: &str,
) -> Result<(), Response> {
    debug_assert!(
        SENSITIVE_ACTION_LABELS.contains(&action_label),
        "action_label not in SENSITIVE_ACTION_LABELS"
    );
    let sess = require_session(portal, target)?;

    // Grant source: header, then form field, then cookie.
    let token = header_value(headers, "x-bss-stepup-token")
        .or_else(|| header_value(headers, "x-bss-step-up-token"))
        .or_else(|| {
            form.iter()
                .find(|(k, _)| k == "step_up_token")
                .map(|(_, v)| v.clone())
        })
        .or_else(|| read_cookie(headers, GRANT_COOKIE));

    let referer = safe_referer_path(headers);
    let next = referer.as_deref().unwrap_or(target);

    let ok = match (&state.db, &token) {
        (Some(pool), Some(tok)) if !tok.is_empty() => {
            consume_step_up_token(pool, &sess.id, tok, action_label)
                .await
                .unwrap_or(false)
        }
        _ => false,
    };
    if ok {
        return Ok(());
    }

    // Stash the POST body for replay (best-effort), then bounce.
    if let Some(pool) = &state.db {
        let _ = stash_pending_action(pool, &sess.id, action_label, target, form, None).await;
    }
    Err(bounce_to_step_up(action_label, next))
}

// ── helpers ──────────────────────────────────────────────────────────────────

#[allow(clippy::too_many_arguments)]
fn render_form(
    state: &AppState,
    portal: &PortalSession,
    action_label: &str,
    next_path: &str,
    issued: bool,
    error: Option<&str>,
    status: StatusCode,
) -> Response {
    let mut resp = render(
        state,
        "auth_step_up.html",
        context! {
            action_label => action_label,
            next_path => next_path,
            issued => issued,
            error => error,
            request => request_ctx("/auth/step-up", portal.identity_email()),
        },
    );
    *resp.status_mut() = status;
    resp
}

fn bounce_to_step_up(action_label: &str, next_path: &str) -> Response {
    let loc = format!(
        "/auth/step-up?action={}&next={}",
        urlencode(action_label),
        urlencode(next_path)
    );
    Redirect::to(&loc).into_response()
}

fn build_grant_cookie(token: &str) -> String {
    let settings = bss_portal_auth::Settings::from_env();
    let mut parts = vec![
        format!("{GRANT_COOKIE}={token}"),
        "Path=/".to_string(),
        "HttpOnly".to_string(),
        "SameSite=Lax".to_string(),
        "Max-Age=60".to_string(),
    ];
    if settings.dev_insecure_cookie == 0 {
        parts.push("Secure".to_string());
    }
    parts.join("; ")
}

fn user_agent(headers: &HeaderMap) -> Option<String> {
    headers
        .get(axum::http::header::USER_AGENT)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string)
}

fn header_value(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get(name)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string)
}

/// The safe same-origin path+query from the `Referer` header, or `None`. Port of
/// `_safe_referer_path`: rejects a different host and anything that doesn't
/// survive `safe_next_path`.
fn safe_referer_path(headers: &HeaderMap) -> Option<String> {
    let raw = header_value(headers, "referer")?;
    // Split scheme://host/path?query. A relative Referer has no "://".
    let after_scheme = raw.split_once("://").map(|(_, r)| r).unwrap_or(&raw);
    let (netloc, path_and_query) = match after_scheme.find('/') {
        Some(i) => (&after_scheme[..i], &after_scheme[i..]),
        None => (after_scheme, "/"),
    };
    // Reject a cross-origin Referer.
    if raw.contains("://") {
        let host = header_value(headers, "host").unwrap_or_default();
        if netloc != host {
            return None;
        }
    }
    if path_and_query.is_empty() {
        return None;
    }
    let candidate = path_and_query.to_string();
    if safe_next_path(Some(&candidate), "") == candidate {
        Some(candidate)
    } else {
        None
    }
}

fn read_cookie(headers: &HeaderMap, name: &str) -> Option<String> {
    let header = headers.get(axum::http::header::COOKIE)?.to_str().ok()?;
    for pair in header.split(';') {
        if let Some((k, v)) = pair.trim().split_once('=') {
            if k.trim() == name {
                return Some(v.trim().to_string());
            }
        }
    }
    None
}

/// Minimal query-component percent-encoding.
fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~' | b'/') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}
