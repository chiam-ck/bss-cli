//! com — Commercial Order Management (Phase 3, port of `services/com`).
//!
//! TMF622 ProductOrder FSM (create → submit → completed/failed/cancelled), price
//! snapshot at order time, and the v1.1 promo consume lifecycle at activation
//! (claim → redeem / revoke). Runs the **outbox relay** (its staged events' only
//! publisher) and two **safe consumers** (`service_order.completed/failed`) plus
//! the reconciliation sweeper — the P2 lapin/sqlx bindings, wired for com.
#![forbid(unsafe_code)]

pub mod config;
pub mod consumer;
pub mod domain;
pub mod error;
pub mod events;
pub mod policies;
pub mod reconciliation;
pub mod repo;
pub mod routes;
pub mod schemas;
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

/// Build the axum app — port of `app.main.create_app`. Health at root, TMF622
/// ProductOrder routes, `/admin-api/v1` reset + clock, `/audit-api/v1` events;
/// token gate outermost, context inside.
pub fn create_app(state: AppState, token_map: Arc<TokenMap>) -> Router {
    let pool = state.pool.clone();

    let stateful: Router = Router::new()
        .merge(routes::health_router())
        .merge(routes::order_router())
        .with_state(state);

    stateful
        .nest("/admin-api/v1", com_reset_router(pool.clone()))
        .nest("/admin-api/v1", clock_admin_router())
        .nest("/audit-api/v1", audit_events_router(pool))
        .layer(from_fn(propagate_context))
        .layer(from_fn_with_state(token_map, require_api_token))
        .layer(from_fn(otel_http_span))
}

/// Operational-data reset — wipes the `order_mgmt` schema (children first). Port
/// of `app.api.admin`.
fn com_reset_router(pool: bss_db::PgPool) -> Router {
    admin_reset_router(
        pool,
        "com",
        vec![ResetPlan::new(
            "order_mgmt",
            vec![
                TableReset::truncate("order_state_history"),
                TableReset::truncate("order_item"),
                TableReset::truncate("product_order"),
            ],
        )],
    )
}
