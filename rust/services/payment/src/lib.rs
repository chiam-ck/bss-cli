//! payment — Phase 4 service (port of `services/payment`).
//!
//! TMF676 payment + paymentMethod surfaces, the v0.16 tokenizer seam (mock |
//! Stripe via direct reqwest, Decision D4), idempotency-keyed off-session
//! charges, and the Stripe webhook receiver (reconcile / drift / record-only).
//! HTTP-only: no MQ, no consumer, **no relay** — `events::stage` writes the
//! `audit.domain_event` row (`published_to_mq = false`) and returns, exactly like
//! the oracle's `publisher.publish`.
#![forbid(unsafe_code)]

pub mod config;
pub mod domain;
pub mod error;
pub mod events;
pub mod policies;
pub mod repo;
pub mod routes;
pub mod schemas;
pub mod select;
pub mod service;
pub mod state;
pub mod tokenizer;
pub mod webhooks;

use std::sync::Arc;

use axum::{
    middleware::{from_fn, from_fn_with_state},
    Router,
};
use bss_admin::{admin_reset_router, ResetPlan, TableReset};
use bss_clock::clock_admin_router;
use bss_context::propagate_context;
use bss_events::audit_events_router;
use bss_middleware::{require_api_token, TokenMap};

use crate::state::AppState;

/// Build the axum app — port of `app.main.create_app`. Health at root, the two
/// TMF676 surfaces, `/admin-api/v1` reset + cutover/ensure + clock,
/// `/audit-api/v1` events, and `/webhooks/stripe` (exempt from the token gate by
/// path). Token gate outermost, context inside.
pub fn create_app(state: AppState, token_map: Arc<TokenMap>) -> Router {
    let pool = state.pool.clone();

    let stateful: Router = Router::new()
        .merge(routes::health_router())
        .merge(routes::payment_router())
        .merge(routes::payment_method_router())
        .merge(routes::webhooks_router())
        .with_state(state.clone());

    // Admin: the shared reset router + payment's cutover/ensure routes, both
    // under /admin-api/v1 (the reset router carries its own state; the extras
    // carry AppState).
    let admin_extra = routes::admin_extra_router().with_state(state);

    stateful
        .nest("/admin-api/v1", payment_reset_router(pool.clone()))
        .nest("/admin-api/v1", admin_extra)
        .nest("/admin-api/v1", clock_admin_router())
        .nest("/audit-api/v1", audit_events_router(pool))
        .layer(from_fn(propagate_context))
        .layer(from_fn_with_state(token_map, require_api_token))
}

/// Operational-data reset — wipes the `payment` schema. Port of `app.api.admin`'s
/// `_OPERATIONAL` (payment_attempt + payment_method).
fn payment_reset_router(pool: bss_db::PgPool) -> Router {
    admin_reset_router(
        pool,
        "payment",
        vec![ResetPlan::new(
            "payment",
            vec![
                TableReset::truncate("payment_attempt"),
                TableReset::truncate("payment_method"),
            ],
        )],
    )
}
