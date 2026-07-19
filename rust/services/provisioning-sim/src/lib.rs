//! provisioning-sim — HLR/PCRF/OCS/SM-DP+ stand-in (Phase 2, port of
//! `services/provisioning-sim`).
//!
//! Consumer + worker heavy: consumes `provisioning.task.created`, runs the
//! fault-injecting worker (`fail_always` / `fail_first_attempt` / `slow` /
//! `stuck`) with configurable per-task-type latency + the eSIM SM-DP+ seam, and
//! inline-publishes `provisioning.task.{completed,failed,stuck}`. The HTTP surface
//! exposes task reads + stuck-resolve / failed-retry + fault-rule CRUD.
#![forbid(unsafe_code)]

pub mod config;
pub mod consumer;
pub mod domain;
pub mod error;
pub mod esim;
pub mod repo;
pub mod routes;
pub mod service;
pub mod state;
pub mod worker;

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

/// Build the axum app — port of `app.main.create_app`. Health at root, the
/// provisioning API under `/provisioning-api/v1`, `/admin-api/v1` reset + clock,
/// `/audit-api/v1` events; token gate outermost, context inside.
pub fn create_app(state: AppState, token_map: Arc<TokenMap>) -> Router {
    let pool = state.pool.clone();

    let stateful: Router = Router::new()
        .merge(routes::health_router())
        .nest("/provisioning-api/v1", routes::provisioning_router())
        .with_state(state);

    stateful
        .nest("/admin-api/v1", provisioning_reset_router(pool.clone()))
        .nest("/admin-api/v1", clock_admin_router())
        .nest("/audit-api/v1", audit_events_router(pool))
        .layer(from_fn(propagate_context))
        .layer(from_fn_with_state(token_map, require_api_token))
        .layer(from_fn(otel_http_span))
}

/// Operational-data reset — wipes `provisioning.provisioning_task`.
/// `fault_injection` is reference data and deliberately not listed (port of
/// `app.api.admin`).
fn provisioning_reset_router(pool: bss_db::PgPool) -> Router {
    admin_reset_router(
        pool,
        "provisioning-sim",
        vec![ResetPlan::new(
            "provisioning",
            vec![TableReset::truncate("provisioning_task")],
        )],
    )
}
