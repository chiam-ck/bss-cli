//! catalog — Phase 3 service (port of `services/catalog`).
//!
//! TMF620 read surface (offering/price/spec) + VAS reads + admin write paths +
//! the v1.1 promotion subsystem (two-system saga over the external loyalty-cli).
//! HTTP-only: no MQ, no consumer, no audit/reset router — just a Postgres pool and
//! an **optional** `LoyaltyClient` (promo subsystem OFF when the token is unset).
//! The fattest client-consumer surface, so its wire shapes protect everyone
//! downstream (rating, com, portals).
#![forbid(unsafe_code)]

pub mod config;
pub mod error;
pub mod money;
pub mod promo_repo;
pub mod promo_service;
pub mod repo;
pub mod routes;
pub mod services;
pub mod state;
pub mod tmf;

use std::sync::Arc;

use axum::{
    middleware::{from_fn, from_fn_with_state},
    Router,
};
use bss_clock::clock_admin_router;
use bss_context::propagate_context;
use bss_middleware::{otel_http_span, require_api_token, TokenMap};

use crate::state::AppState;

/// Build the axum app — port of `bss_catalog.app.create_app`. Mirrors the router
/// mounts (health at root, absolute TMF/VAS/admin/promotion paths, `/admin-api/v1`
/// clock) and the middleware order (token gate outermost, context inside).
pub fn create_app(state: AppState, token_map: Arc<TokenMap>) -> Router {
    let stateful: Router = Router::new()
        .merge(routes::health_router())
        .merge(routes::tmf_router())
        .merge(routes::vas_router())
        .merge(routes::admin_router())
        .merge(routes::promotion_router())
        .with_state(state);

    stateful
        .nest("/admin-api/v1", clock_admin_router())
        .layer(from_fn(propagate_context))
        .layer(from_fn_with_state(token_map, require_api_token))
        .layer(from_fn(otel_http_span))
}
