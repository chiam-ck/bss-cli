//! Slice-1 route handlers: the public static surface. No BSS reads, no session.

use axum::extract::State;
use axum::http::{header, StatusCode};
use axum::response::{Html, IntoResponse, Json, Response};
use axum::Extension;
use minijinja::{context, Value};
use serde_json::json;

use crate::middleware::PortalSession;
use crate::offerings::flatten_offerings;
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
pub async fn welcome(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
) -> Response {
    render(
        &state,
        "welcome.html",
        context! {
            is_signed_in => portal.identity.is_some(),
            request => request_ctx("/welcome", portal.identity_email()),
        },
    )
}

/// `GET /plans` — public catalog browse. Flattens the TMF offerings into plan
/// cards, cheapest-first. Anonymous CTAs bounce through `/auth/login`.
pub async fn plans(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
) -> Response {
    let plans = match &state.clients {
        Some(c) => match c.catalog.list_offerings().await {
            Ok(raw) => {
                let arr = raw.as_array().cloned().unwrap_or_default();
                flatten_offerings(&arr)
            }
            Err(err) => {
                tracing::warn!(error = %err, "portal.plans.catalog_read_failed");
                Vec::new()
            }
        },
        None => Vec::new(),
    };
    render(
        &state,
        "plans.html",
        context! {
            plans => Value::from_serialize(&plans),
            is_signed_in => portal.identity.is_some(),
            request => request_ctx("/plans", portal.identity_email()),
        },
    )
}

/// `GET /terms` — public legal page.
pub async fn terms(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
) -> Response {
    render(
        &state,
        "legal_terms.html",
        context! { request => request_ctx("/terms", portal.identity_email()) },
    )
}

/// `GET /privacy` — public legal page.
pub async fn privacy(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
) -> Response {
    render(
        &state,
        "legal_privacy.html",
        context! { request => request_ctx("/privacy", portal.identity_email()) },
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
