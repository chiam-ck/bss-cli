//! mediation — TMF635 online mediation (Phase 2, port of `services/mediation`).
//!
//! Block-at-edge ingress: it does *not* consume from MQ — it's the HTTP producer
//! that turns an ingress CDR into a persisted `usage_event` + an inline-published
//! `usage.recorded` (the event rating consumes). A blocked/unknown subscription is
//! rejected 422 with no `usage_event` row and a `usage.rejected` audit trace.
//!
//! Second Phase-2 stamp of the porting playbook: adds the first service-owned
//! table write, the `SubscriptionClient`, and the shared `bss-admin` reset router.
#![forbid(unsafe_code)]

pub mod config;
pub mod domain;
pub mod error;
pub mod repo;
pub mod routes;
pub mod service;
pub mod state;

use std::sync::Arc;

use axum::{
    middleware::{from_fn, from_fn_with_state},
    Router,
};
use bss_admin::{admin_reset_router, ResetPlan, TableReset};
use bss_clock::clock_admin_router;
use bss_context::propagate_context;
use bss_events::audit_events_router;
use bss_middleware::{otel_http_span, require_api_token, TokenMap};

use crate::state::AppState;

/// Build the axum app — port of `app.main.create_app`. Mirrors the Python router
/// mounts (health at root, TMF635 usage under `/tmf-api/usageManagement/v4`,
/// `/admin-api/v1` reset + clock, `/audit-api/v1` events) and the middleware order
/// (token gate outermost, context inside).
pub fn create_app(state: AppState, token_map: Arc<TokenMap>) -> Router {
    let pool = state.pool.clone();

    // Stateful routes (health + usage) carry AppState; finalize before nesting
    // the stateless shared routers.
    let stateful: Router = Router::new()
        .merge(routes::health_router())
        .nest("/tmf-api/usageManagement/v4", routes::usage_router())
        .with_state(state);

    stateful
        .nest("/admin-api/v1", mediation_reset_router(pool.clone()))
        .nest("/admin-api/v1", clock_admin_router())
        .nest("/audit-api/v1", audit_events_router(pool))
        .layer(from_fn(propagate_context))
        .layer(from_fn_with_state(token_map, require_api_token))
        .layer(from_fn(otel_http_span))
}

/// The operational-data reset plan — wipes `mediation.usage_event` (port of
/// `app.api.admin`).
fn mediation_reset_router(pool: bss_db::PgPool) -> Router {
    admin_reset_router(
        pool,
        "mediation",
        vec![ResetPlan::new(
            "mediation",
            vec![TableReset::truncate("usage_event")],
        )],
    )
}
