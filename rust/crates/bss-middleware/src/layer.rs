//! Perimeter token middleware — require `X-BSS-API-Token` on every request.
//!
//! Port of `BSSApiTokenMiddleware`:
//! 1. exempt path (`/health*`, `/webhooks/…`) → pass through;
//! 2. missing header → 401 `AUTH_MISSING_TOKEN`;
//! 3. present but no map entry → 401 `AUTH_INVALID_TOKEN` (constant-time);
//! 4. match → insert the resolved [`ServiceIdentity`] into extensions (never from
//!    a header — guard #6) and pass through.
//!
//! Stacked *outside* `bss_context::propagate_context`, which reads the
//! `ServiceIdentity` this layer set. Wire it with
//! `from_fn_with_state(Arc<TokenMap>, require_api_token)`.

use std::sync::Arc;

use axum::{
    extract::{Request, State},
    http::StatusCode,
    middleware::Next,
    response::{IntoResponse, Response},
    Json,
};
use bss_context::ServiceIdentity;
use serde_json::json;

use crate::token_map::TokenMap;

pub const AUTH_MISSING_TOKEN: &str = "AUTH_MISSING_TOKEN";
pub const AUTH_INVALID_TOKEN: &str = "AUTH_INVALID_TOKEN";

const TOKEN_HEADER: &str = "x-bss-api-token";

/// Exact-match exempt paths + the `/webhooks/` prefix. Mirrors `EXEMPT_PATHS`
/// and `WEBHOOK_EXEMPT_PATHS`. `/healthz` and bare `/webhooks` are **not** exempt.
fn is_exempt(path: &str) -> bool {
    matches!(path, "/health" | "/health/ready" | "/health/live") || path.starts_with("/webhooks/")
}

fn unauthorized(code: &str, message: &str) -> Response {
    (
        StatusCode::UNAUTHORIZED,
        Json(json!({ "code": code, "message": message })),
    )
        .into_response()
}

/// Middleware fn for `axum::middleware::from_fn_with_state(Arc<TokenMap>, …)`.
pub async fn require_api_token(
    State(token_map): State<Arc<TokenMap>>,
    mut req: Request,
    next: Next,
) -> Response {
    if is_exempt(req.uri().path()) {
        return next.run(req).await;
    }

    let provided = req
        .headers()
        .get(TOKEN_HEADER)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if provided.is_empty() {
        return unauthorized(AUTH_MISSING_TOKEN, "X-BSS-API-Token header required");
    }

    match token_map.lookup(provided) {
        None => unauthorized(AUTH_INVALID_TOKEN, "invalid API token"),
        Some(identity) => {
            // Authoritative service_identity: only from token validation, never
            // a client header (guard #6, structural).
            req.extensions_mut().insert(ServiceIdentity(identity));
            next.run(req).await
        }
    }
}
