//! rating — Phase 1 pilot service (port of `services/rating`).
//!
//! Stateless: a pure `rate_usage` over a JSON tariff, exposed both as a debug
//! HTTP surface and as the `usage.recorded → usage.rated` consumer. This crate is
//! where the per-service porting pattern is stamped first (see the Phase-1
//! playbook in `phases/2.0/`): app factory wired with the token + context layers,
//! the first typed `bss-clients` client (catalog), the lapin consumer with inline
//! publish, and the shared `/admin-api/v1` + `/audit-api/v1` mounts.
#![forbid(unsafe_code)]

pub mod config;
pub mod consumer;
pub mod domain;
pub mod error;
pub mod routes;
pub mod state;

use std::sync::Arc;

use axum::{
    middleware::{from_fn, from_fn_with_state},
    Router,
};
use bss_clock::clock_admin_router;
use bss_context::propagate_context;
use bss_events::audit_events_router;
use bss_middleware::{otel_http_span, require_api_token, TokenMap};

use crate::state::AppState;

/// Build the axum app — port of `app.main.create_app`. Mirrors the Python router
/// mounts (health at root, `/rating-api/v1`, `/admin-api/v1` clock, `/audit-api/v1`
/// events) and the middleware order (token gate outermost, context inside).
pub fn create_app(state: AppState, token_map: Arc<TokenMap>) -> Router {
    let pool = state.pool.clone();

    // The rating + health routes carry AppState; finalize them to `Router<()>`
    // before nesting the stateless shared routers.
    let stateful: Router = Router::new()
        .merge(routes::health_router())
        .nest("/rating-api/v1", routes::rating_router())
        .with_state(state);

    stateful
        .nest("/admin-api/v1", clock_admin_router())
        .nest("/audit-api/v1", audit_events_router(pool))
        // Context layer reads the ServiceIdentity the token layer stashed; token
        // layer is added last so it runs first (outermost).
        .layer(from_fn(propagate_context))
        .layer(from_fn_with_state(token_map, require_api_token))
        .layer(from_fn(otel_http_span))
}
