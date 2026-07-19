//! Read-only account pages: `/billing/history` + `/esim/{id}`. Port of
//! `bss_self_serve.routes.billing` + `.esim`. No step-up, no audit (reads).
//! `customer_id` is bound from the verified session; ownership is rechecked on
//! the esim page (the URL is shareable).

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Extension;
use minijinja::context;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::deps::require_linked_customer;
use crate::error_messages::render as render_rule;
use crate::middleware::PortalSession;
use crate::qrpng::activation_qr_data_uri;
use crate::routes::render;
use crate::templating::request_ctx;
use crate::AppState;

const PAGE_SIZE: i64 = 20;
const SUB_OWNERSHIP_RULE: &str = "policy.ownership.subscription_not_owned";

// ── GET /billing/history ─────────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct BillingQuery {
    #[serde(default)]
    page: i64,
}

fn purpose_label(purpose: &str) -> String {
    match purpose {
        "subscription" | "subscription_renewal" => "Subscription renewal".to_string(),
        "subscription_activation" => "New line activation".to_string(),
        "vas" | "vas_purchase" => "Add-on / top-up".to_string(),
        "card_change" => "Payment method change".to_string(),
        other => {
            let mut s = other.replace('_', " ");
            if let Some(c) = s.get_mut(0..1) {
                c.make_ascii_uppercase();
            }
            s
        }
    }
}

pub async fn history(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Query(q): Query<BillingQuery>,
) -> Response {
    let customer_id = match require_linked_customer(&portal, "/billing/history") {
        Ok(c) => c,
        Err(r) => return r,
    };
    let page = q.page.clamp(0, 10_000);
    let offset = page * PAGE_SIZE;

    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };

    let attempts = clients
        .payment
        .list_payments(Some(&customer_id), None, PAGE_SIZE, offset)
        .await
        .ok()
        .and_then(|v| v.as_array().cloned())
        .unwrap_or_default();
    let total = clients
        .payment
        .count_payments(&customer_id)
        .await
        .ok()
        .map(|v| {
            v.as_i64()
                .or_else(|| v.get("count").and_then(Value::as_i64))
                .unwrap_or(0)
        })
        .unwrap_or(0);
    let methods = clients
        .payment
        .list_methods(&customer_id)
        .await
        .ok()
        .and_then(|v| v.as_array().cloned())
        .unwrap_or_default();

    // payment_method_id → last-4 (nested under cardSummary).
    let last4_for = |mid: &str| -> String {
        methods
            .iter()
            .find(|m| m.get("id").and_then(Value::as_str) == Some(mid))
            .map(|m| {
                m.get("cardSummary")
                    .and_then(|cs| cs.get("last4"))
                    .or_else(|| m.get("last4"))
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string()
            })
            .unwrap_or_default()
    };

    let rows: Vec<Value> = attempts
        .iter()
        .map(|a| {
            let method_id = a.get("paymentMethodId").and_then(Value::as_str);
            let purpose = a.get("purpose").and_then(Value::as_str).unwrap_or("");
            json!({
                "id": a.get("id"),
                "attempted_at": a.get("attemptedAt"),
                "amount": a.get("amount"),
                "currency": a.get("currency").and_then(Value::as_str).unwrap_or("SGD"),
                "status": a.get("status"),
                "purpose": purpose,
                "purpose_label": purpose_label(purpose),
                "method_last4": method_id.map(last4_for).unwrap_or_default(),
                "method_id": method_id,
                "decline_reason": a.get("declineReason"),
                "gateway_ref": a.get("gatewayRef"),
            })
        })
        .collect();

    let pages = ((total + PAGE_SIZE - 1) / PAGE_SIZE).max(1);
    let has_prev = page > 0;
    let has_next = (page + 1) < pages && attempts.len() as i64 == PAGE_SIZE;

    render(
        &state,
        "billing_history.html",
        context! {
            rows => minijinja::Value::from_serialize(&rows),
            page => page,
            page_human => page + 1,
            pages => pages,
            has_prev => has_prev,
            has_next => has_next,
            total => total,
            request => request_ctx("/billing/history", portal.identity_email()),
        },
    )
}

// ── GET /esim/{subscription_id} ──────────────────────────────────────────────

fn last4_dots(value: Option<&str>) -> String {
    match value {
        Some(v) if v.len() > 4 => format!("…{}", &v[v.len() - 4..]),
        Some(v) if !v.is_empty() => v.to_string(),
        _ => "----".to_string(),
    }
}

pub async fn esim_view(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Path(subscription_id): Path<String>,
) -> Response {
    let customer_id = match require_linked_customer(&portal, "/") {
        Ok(c) => c,
        Err(r) => return r,
    };
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };

    let forbidden = |state: &AppState| -> Response {
        let mut resp = render(
            state,
            "esim_forbidden.html",
            context! {
                customer_facing => render_rule(SUB_OWNERSHIP_RULE),
                request => request_ctx("/", portal.identity_email()),
            },
        );
        *resp.status_mut() = StatusCode::FORBIDDEN;
        resp
    };

    let sub = match clients.subscription.get(&subscription_id).await {
        Ok(s) => s,
        Err(_) => return forbidden(&state),
    };
    if sub.get("customerId").and_then(Value::as_str) != Some(customer_id.as_str()) {
        return forbidden(&state);
    }

    let iccid = sub.get("iccid").and_then(Value::as_str);
    let mut activation_code: Option<String> = None;
    let mut smdp_server: Option<String> = None;
    let mut matching_id: Option<String> = None;
    if let Some(iccid) = iccid {
        if let Ok(activation) = clients.inventory.get_activation_code(iccid).await {
            let get = |k1: &str, k2: &str| {
                activation
                    .get(k1)
                    .or_else(|| activation.get(k2))
                    .and_then(Value::as_str)
                    .map(str::to_string)
            };
            activation_code = get("activation_code", "activationCode");
            smdp_server = get("smdp_server", "smdpServer");
            matching_id = get("matching_id", "matchingId");
        }
    }
    let qr = activation_code
        .as_deref()
        .map(activation_qr_data_uri)
        .unwrap_or_default();

    render(
        &state,
        "esim.html",
        context! {
            subscription => sub.clone(),
            activation_code => activation_code,
            smdp_server => smdp_server,
            matching_id => matching_id,
            qr_data_uri => qr,
            iccid_last4 => last4_dots(iccid),
            imsi_last4 => last4_dots(sub.get("imsi").and_then(Value::as_str)),
            msisdn => sub.get("msisdn").and_then(Value::as_str),
            state => sub.get("state").and_then(Value::as_str),
            request => request_ctx("/", portal.identity_email()),
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn purpose_labels() {
        assert_eq!(
            purpose_label("subscription_renewal"),
            "Subscription renewal"
        );
        assert_eq!(purpose_label("vas"), "Add-on / top-up");
        assert_eq!(purpose_label("some_thing"), "Some thing");
    }

    #[test]
    fn last4_formatting() {
        assert_eq!(last4_dots(Some("8991000012345")), "…2345");
        assert_eq!(last4_dots(Some("12")), "12");
        assert_eq!(last4_dots(None), "----");
    }
}
