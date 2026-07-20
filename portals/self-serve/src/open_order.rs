//! "My open order" — the customer-facing screen for a pending (incomplete)
//! signup order (v-reservation phase 3). Low-prominence: linked from the
//! dashboard only when one exists.
//!
//! **Ownership-bound (`*.mine`, v0.12 doctrine):** the open order is keyed on the
//! session identity's verified email — never an id/email from the request. Cancel
//! derives the id from the caller's own open order, so a customer can only ever
//! view or cancel their own.

use axum::extract::State;
use axum::response::{IntoResponse, Redirect, Response};
use axum::Extension;
use minijinja::context;
use serde_json::Value;

use crate::deps::require_verified_email;
use crate::middleware::PortalSession;
use crate::routes::render;
use crate::templating::request_ctx;
use crate::AppState;

/// `GET /account/open-order` — the caller's pending order (if any) with Resume +
/// Cancel. Section-degrading: no open order → a friendly empty state.
pub async fn open_order_view(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
) -> Response {
    let identity = match require_verified_email(&portal, "/account/open-order") {
        Ok(i) => i,
        Err(r) => return r,
    };
    let Some(clients) = &state.clients else {
        return (axum::http::StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };
    let open_order = clients
        .inventory
        .open_order_by_identity(&identity.email)
        .await
        .ok()
        .filter(|v| !v.is_null());

    render(
        &state,
        "open_order.html",
        context! {
            open_order => minijinja::Value::from_serialize(&open_order),
            request => request_ctx("/account/open-order", Some(&identity.email)),
        },
    )
}

/// `POST /account/open-order/cancel` — cancel the caller's OWN open order,
/// releasing the held number so they can start fresh. The id is looked up from
/// the session identity, never taken from the request body.
pub async fn open_order_cancel(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
) -> Response {
    let identity = match require_verified_email(&portal, "/account/open-order") {
        Ok(i) => i,
        Err(r) => return r,
    };
    let Some(clients) = &state.clients else {
        return (axum::http::StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };
    if let Ok(oo) = clients
        .inventory
        .open_order_by_identity(&identity.email)
        .await
    {
        if let Some(id) = oo.get("id").and_then(Value::as_str) {
            if let Err(e) = clients.inventory.cancel_open_order(id).await {
                tracing::warn!(error = %e, open_order_id = %id, "portal.open_order.cancel_failed");
            }
        }
    }
    Redirect::to("/account/open-order").into_response()
}
