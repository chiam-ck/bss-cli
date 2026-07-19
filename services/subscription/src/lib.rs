//! subscription — Subscription & Bundle Balance (Phase 4b, port of
//! `services/subscription`).
//!
//! The subscription FSM (pending/active/blocked/terminated), block-on-exhaust
//! balance decrement under `FOR UPDATE` (the `usage.rated` consumer), price-
//! snapshot renewal + plan-change/price-migration pivot, VAS top-up, and the
//! in-process **renewal worker**. Runs the **outbox relay** (its staged events'
//! only publisher) and the **safe consumer** — the P2 lapin/sqlx bindings, wired
//! for subscription.
#![forbid(unsafe_code)]

pub mod config;
pub mod consumer;
pub mod domain;
pub mod error;
pub mod events;
pub mod money;
pub mod policies;
pub mod repo;
pub mod routes;
pub mod schemas;
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
/// subscription API under `/subscription-api/v1`, `/admin-api/v1` reset + clock +
/// renewal-tick, `/audit-api/v1` events. Token gate outermost, context inside.
pub fn create_app(state: AppState, token_map: Arc<TokenMap>) -> Router {
    let pool = state.pool.clone();

    let stateful: Router = Router::new()
        .merge(routes::health_router())
        .merge(routes::subscription_router())
        .with_state(state.clone());

    let admin_extra = routes::admin_extra_router().with_state(state);

    stateful
        .nest("/admin-api/v1", subscription_reset_router(pool.clone()))
        .nest("/admin-api/v1", admin_extra)
        .nest("/admin-api/v1", clock_admin_router())
        .nest("/audit-api/v1", audit_events_router(pool))
        .layer(from_fn(propagate_context))
        .layer(from_fn_with_state(token_map, require_api_token))
        .layer(from_fn(otel_http_span))
}

/// Operational-data reset — wipes the `subscription` schema (children first). Port
/// of `app.api.admin`'s `_OPERATIONAL`.
fn subscription_reset_router(pool: bss_db::PgPool) -> Router {
    admin_reset_router(
        pool,
        "subscription",
        vec![ResetPlan::new(
            "subscription",
            vec![
                TableReset::truncate("subscription_state_history"),
                TableReset::truncate("vas_purchase"),
                TableReset::truncate("bundle_balance"),
                TableReset::truncate("subscription"),
            ],
        )],
    )
}
