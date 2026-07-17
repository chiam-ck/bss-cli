//! Shared route helpers + the health endpoint.

use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{Html, IntoResponse, Response};
use minijinja::Value;

use crate::AppState;

/// Render `template` with `ctx`, or a 500 when the template errors. Mirrors the
/// self-serve portal's helper.
pub fn render(state: &AppState, template: &str, ctx: Value) -> Response {
    match state.env.get_template(template) {
        Ok(tpl) => match tpl.render(ctx) {
            Ok(html) => Html(html).into_response(),
            Err(e) => {
                tracing::error!(template = %template, error = %e, "cockpit.template.render_failed");
                (StatusCode::INTERNAL_SERVER_ERROR, "template render failed").into_response()
            }
        },
        Err(e) => {
            tracing::error!(template = %template, error = %e, "cockpit.template.missing");
            (StatusCode::INTERNAL_SERVER_ERROR, "template not found").into_response()
        }
    }
}

/// `GET /health`.
pub async fn health(State(state): State<AppState>) -> Response {
    axum::Json(serde_json::json!({
        "status": "ok",
        "service": state.settings.service_name,
        "version": state.settings.version,
    }))
    .into_response()
}
