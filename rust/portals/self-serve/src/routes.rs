//! Slice-1 route handlers: the public static surface. No BSS reads, no session.

use axum::extract::State;
use axum::http::{header, StatusCode};
use axum::response::{Html, IntoResponse, Json, Response};
use minijinja::{context, Value};
use serde_json::json;

use crate::templating::request_ctx;
use crate::AppState;

/// Render a template to an `Html` response, or a 500 with the error (logged).
fn render(state: &AppState, name: &str, ctx: Value) -> Response {
    match state.env.get_template(name).and_then(|t| t.render(ctx)) {
        Ok(html) => Html(html).into_response(),
        Err(err) => {
            tracing::error!(template = name, error = %err, "portal.render_failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                format!("template render error: {err}"),
            )
                .into_response()
        }
    }
}

/// `GET /health` — liveness probe (mirrors the Python JSON shape).
pub async fn health(State(state): State<AppState>) -> Json<serde_json::Value> {
    Json(json!({
        "status": "ok",
        "service": state.settings.service_name,
        "version": state.settings.version,
    }))
}

/// `GET /welcome` — public marketing landing (anonymous or signed-in CTAs).
pub async fn welcome(State(state): State<AppState>) -> Response {
    render(
        &state,
        "welcome.html",
        context! {
            is_signed_in => false,
            request => request_ctx("/welcome", None),
        },
    )
}

/// `GET /terms` — public legal page.
pub async fn terms(State(state): State<AppState>) -> Response {
    render(
        &state,
        "legal_terms.html",
        context! { request => request_ctx("/terms", None) },
    )
}

/// `GET /privacy` — public legal page.
pub async fn privacy(State(state): State<AppState>) -> Response {
    render(
        &state,
        "legal_privacy.html",
        context! { request => request_ctx("/privacy", None) },
    )
}

/// `GET /branding/logo` — the operator's uploaded logo (404 when none). Public
/// by design; the URL carries `?v=<mtime>` so immutable caching is safe.
pub async fn branding_logo() -> Response {
    match bss_branding::logo_http() {
        Some(logo) => (
            [
                (header::CONTENT_TYPE, logo.content_type.to_string()),
                (header::CACHE_CONTROL, logo.cache_control.to_string()),
                (header::ETAG, logo.etag),
            ],
            logo.bytes,
        )
            .into_response(),
        None => StatusCode::NOT_FOUND.into_response(),
    }
}
