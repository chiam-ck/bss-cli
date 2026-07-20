//! crm — CRM + Inventory (Phase 4c, the last service; port of `services/crm`).
//!
//! TMF629 customer / TMF621 ticket / TMF683 interaction + the Case/Ticket/
//! PortRequest FSMs + KYC attestation + the MSISDN/eSIM pools (the cross-service
//! inventory contract subscription/som call). **HTTP-only, stage-only events** —
//! no relay, no consumer, no MQ (the lifespan opens no broker), exactly like the
//! oracle. Owns two schemas: `crm` (operational truncate) + `inventory` (pool
//! UPDATE-reset).
#![forbid(unsafe_code)]

pub mod config;
pub mod domain;
pub mod error;
pub mod events;
pub mod policies;
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

/// Build the axum app — port of `app.main.create_app`. Health at root; TMF629/621/
/// 683 + `/crm-api/v1` (case/kyc/agent/chat/port) + `/inventory-api/v1`;
/// `/admin-api/v1` reset + clock; `/audit-api/v1` events. Token gate outermost,
/// context inside.
pub fn create_app(state: AppState, token_map: Arc<TokenMap>) -> Router {
    let pool = state.pool.clone();

    let stateful: Router = Router::new()
        .merge(routes::health_router())
        .merge(routes::customer_router())
        .merge(routes::ticket_router())
        .merge(routes::interaction_router())
        .merge(routes::crm_router())
        .merge(routes::inventory_router())
        .with_state(state);

    stateful
        .nest("/admin-api/v1", crm_reset_router(pool.clone()))
        .nest("/admin-api/v1", clock_admin_router())
        .nest("/audit-api/v1", audit_events_router(pool))
        .layer(from_fn(propagate_context))
        .layer(from_fn_with_state(token_map, require_api_token))
        .layer(from_fn(otel_http_span))
}

/// Operational-data reset — port of `app.api.admin`. Wipes the `crm` schema
/// (children first; `agent` + `sla_policy` are reference and untouched) and
/// **update-resets** the `inventory` pools (rows kept, assignment cleared).
fn crm_reset_router(pool: bss_db::PgPool) -> Router {
    admin_reset_router(
        pool,
        "crm",
        vec![
            ResetPlan::new(
                "crm",
                vec![
                    TableReset::truncate("ticket_state_history"),
                    TableReset::truncate("ticket"),
                    TableReset::truncate("case_note"),
                    TableReset::truncate("case"),
                    TableReset::truncate("interaction"),
                    TableReset::truncate("customer_identity"),
                    TableReset::truncate("customer"),
                    TableReset::truncate("contact_medium"),
                    TableReset::truncate("individual"),
                    TableReset::truncate("party"),
                ],
            ),
            ResetPlan::new(
                "inventory",
                vec![
                    TableReset::update(
                        "msisdn_pool",
                        "UPDATE \"inventory\".\"msisdn_pool\" SET status = 'available', \
                         reserved_at = NULL, reserved_until = NULL, reserved_for = NULL, \
                         assigned_to_subscription_id = NULL, quarantine_until = NULL",
                    ),
                    TableReset::update(
                        "esim_profile",
                        "UPDATE \"inventory\".\"esim_profile\" SET profile_state = 'available', \
                         assigned_msisdn = NULL, assigned_to_subscription_id = NULL, reserved_at = NULL, \
                         downloaded_at = NULL, activated_at = NULL",
                    ),
                ],
            ),
        ],
    )
}
