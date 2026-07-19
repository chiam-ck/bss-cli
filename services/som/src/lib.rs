//! som — Service Order Management (Phase 2, port of `services/som`).
//!
//! The event-plane heart of the order pipeline: consumes `order.in_progress`,
//! decomposes it into ServiceOrder → CFS → RFS with atomic MSISDN/eSIM
//! reservation, then drives `provisioning.task.*` to `service_order.completed`.
//! Runs the **outbox relay** (its staged events' only publisher) and the four
//! **safe (retry/park + inbox-dedup) consumers** — the deferred P2 lapin/sqlx
//! bindings land here (see `bss_events::{start_relay, bind_consumer}`).
#![forbid(unsafe_code)]

pub mod config;
pub mod consumer;
pub mod decomposition;
pub mod domain;
pub mod error;
pub mod events;
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

/// Build the axum app — port of `app.main.create_app`. Health at root, TMF641
/// ServiceOrder + TMF638 Service reads, `/admin-api/v1` reset + clock,
/// `/audit-api/v1` events; token gate outermost, context inside.
pub fn create_app(state: AppState, token_map: Arc<TokenMap>) -> Router {
    let pool = state.pool.clone();

    let stateful: Router = Router::new()
        .merge(routes::health_router())
        .nest(
            "/tmf-api/serviceOrderingManagement/v4",
            routes::service_order_router(),
        )
        .nest(
            "/tmf-api/serviceInventoryManagement/v4",
            routes::service_router(),
        )
        .with_state(state);

    stateful
        .nest("/admin-api/v1", som_reset_router(pool.clone()))
        .nest("/admin-api/v1", clock_admin_router())
        .nest("/audit-api/v1", audit_events_router(pool))
        .layer(from_fn(propagate_context))
        .layer(from_fn_with_state(token_map, require_api_token))
        .layer(from_fn(otel_http_span))
}

/// Operational-data reset — wipes the `service_inventory` graph (children first,
/// then parents; truncate-cascade handles FKs). Port of `app.api.admin`.
fn som_reset_router(pool: bss_db::PgPool) -> Router {
    admin_reset_router(
        pool,
        "som",
        vec![ResetPlan::new(
            "service_inventory",
            vec![
                TableReset::truncate("service_state_history"),
                TableReset::truncate("service"),
                TableReset::truncate("service_order_item"),
                TableReset::truncate("service_order"),
            ],
        )],
    )
}
