//! Order screens — cross-customer queue + COM/SOM detail (v1.6 cockpit CRM). Port
//! of `bss_csr.routes.orders`.
//!
//! The queue rides the v1.6 COM extension (`GET /productOrder` without
//! `customerId`). v1.6.1 (operator directive) — full order CRUD is direct: create
//! from the queue page, submit/cancel from the detail page. Submit charges the
//! card-on-file at activation and cancel is on the destructive list, so both sit
//! behind the two-step UI confirm (`confirm=yes`); the COM policy layer stays the
//! server-side gate.

use axum::extract::{Form, Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Redirect, Response};
use bss_clients::ClientError;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::routes::{back_to, render, CONFIRM_REQUIRED};
use crate::views::{field, field_str, flatten_order, fmt_dt};
use crate::AppState;

const PAGE_SIZE: i64 = 25;

const ORDER_STATES: [&str; 7] = [
    "draft",
    "submitted",
    "awaiting_payment",
    "in_progress",
    "completed",
    "cancelled",
    "failed",
];

fn opt(s: &str) -> Option<&str> {
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}

// ── GET /orders ──────────────────────────────────────────────────────

#[derive(Deserialize, Default)]
pub struct ListQuery {
    #[serde(default)]
    customer: String,
    #[serde(default)]
    state: String,
    #[serde(default)]
    page: i64,
    #[serde(default)]
    flash: String,
    #[serde(default)]
    err: String,
}

pub async fn orders_list(State(state): State<AppState>, Query(q): Query<ListQuery>) -> Response {
    if !(0..=10_000).contains(&q.page) {
        return (StatusCode::UNPROCESSABLE_ENTITY, "page out of range").into_response();
    }
    let customer_clean = q.customer.trim().to_string();
    let state_clean = q.state.trim().to_string();

    let mut rows: Vec<Value> = Vec::new();
    let mut has_next = false;
    let mut plans: Vec<String> = Vec::new();

    if let Some(clients) = &state.clients {
        let raw = match clients
            .com
            .list_orders_paged(
                opt(&customer_clean),
                opt(&state_clean),
                Some(PAGE_SIZE + 1),
                Some(q.page * PAGE_SIZE),
            )
            .await
        {
            Ok(v) => v.as_array().cloned().unwrap_or_default(),
            Err(e) => {
                tracing::warn!(status = e.status_code(), "csr.orders.list_failed");
                Vec::new()
            }
        };
        has_next = raw.len() as i64 > PAGE_SIZE;
        rows = raw
            .iter()
            .take(PAGE_SIZE as usize)
            .map(flatten_order)
            .collect();

        // Plan ids for the create-order select — best-effort. `isBundle` defaults
        // to true (a plan) when the field is absent, matching Python's `.get(…, True)`.
        if let Ok(offerings) = clients
            .catalog
            .list_active_offerings(&bss_clock::now().to_rfc3339())
            .await
        {
            plans = offerings
                .as_array()
                .map(|arr| {
                    arr.iter()
                        .filter(|o| o.get("isBundle").and_then(Value::as_bool).unwrap_or(true))
                        .map(|o| {
                            o.get("id")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_string()
                        })
                        .collect()
                })
                .unwrap_or_default();
        }
    }

    render(
        &state,
        "orders_list.html",
        minijinja::Value::from_serialize(json!({
            "active_page": "orders",
            "model": "(env default)",
            "customer": customer_clean,
            "state": state_clean,
            "states": ORDER_STATES,
            "rows": rows,
            "plans": plans,
            "page": q.page,
            "has_prev": q.page > 0,
            "has_next": has_next,
            "flash": q.flash,
            "err": q.err.chars().take(300).collect::<String>(),
        })),
    )
}

#[derive(Deserialize, Default)]
pub struct FlashQuery {
    #[serde(default)]
    flash: String,
    #[serde(default)]
    err: String,
}

// ── writes ───────────────────────────────────────────────────────────

fn back_to_order(order_id: &str, flash: &str, err: &str) -> Response {
    back_to(&format!("/orders/{order_id}"), flash, err)
}

fn run(order_id: &str, action: &str, r: Result<Value, ClientError>) -> Response {
    // COM's error copy says "COM error", not "CRM error" — so map here rather than
    // reusing routes::write_result (which is CRM-worded).
    match r {
        Ok(_) => back_to_order(order_id, action, ""),
        Err(ClientError::Policy(p)) => back_to_order(order_id, "", &p.message),
        Err(e) => back_to_order(order_id, "", &format!("COM error ({})", e.status_code())),
    }
}

#[derive(Deserialize)]
pub struct CreateForm {
    customer_id: String,
    offering_id: String,
    #[serde(default)]
    msisdn_preference: String,
    #[serde(default)]
    discount_code: String,
}

/// `POST /orders/create` — create (not submit); lands on the new order's detail.
pub async fn create_order(State(state): State<AppState>, Form(form): Form<CreateForm>) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let msisdn = form.msisdn_preference.trim();
    let discount = form.discount_code.trim();
    let r = clients
        .com
        .create_order(
            form.customer_id.trim(),
            form.offering_id.trim(),
            if msisdn.is_empty() {
                None
            } else {
                Some(msisdn)
            },
            None, // notes — the create form doesn't collect them
            if discount.is_empty() {
                None
            } else {
                Some(discount)
            },
            false,
        )
        .await;
    match r {
        Ok(order) => {
            let id = order.get("id").and_then(Value::as_str).unwrap_or("");
            back_to_order(id, "order_created", "")
        }
        // A create failure has no order to land on → bounce to the queue.
        Err(ClientError::Policy(p)) => back_to("/orders", "", &p.message),
        Err(e) => back_to("/orders", "", &format!("COM error ({})", e.status_code())),
    }
}

#[derive(Deserialize, Default)]
pub struct ConfirmForm {
    #[serde(default)]
    confirm: String,
}

/// `POST /orders/{id}/submit` — **confirm-gated** (charges the card at activation).
pub async fn submit_order(
    State(state): State<AppState>,
    Path(order_id): Path<String>,
    Form(form): Form<ConfirmForm>,
) -> Response {
    if form.confirm != "yes" {
        return back_to_order(&order_id, "", CONFIRM_REQUIRED);
    }
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients.com.submit_order(&order_id).await;
    run(&order_id, "order_submitted", r)
}

/// `POST /orders/{id}/cancel` — **confirm-gated** (destructive list).
pub async fn cancel_order(
    State(state): State<AppState>,
    Path(order_id): Path<String>,
    Form(form): Form<ConfirmForm>,
) -> Response {
    if form.confirm != "yes" {
        return back_to_order(&order_id, "", CONFIRM_REQUIRED);
    }
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients.com.cancel_order(&order_id).await;
    run(&order_id, "order_cancelled", r)
}

#[derive(Deserialize, Default)]
pub struct JumpQuery {
    #[serde(default)]
    order_id: String,
}

/// `GET /orders/jump` — the id-box shortcut. Empty → back to the queue.
pub async fn orders_jump(Query(q): Query<JumpQuery>) -> Response {
    let target = q.order_id.trim();
    if target.is_empty() {
        Redirect::to("/orders").into_response()
    } else {
        Redirect::to(&format!("/orders/{target}")).into_response()
    }
}

// ── GET /orders/{id} ─────────────────────────────────────────────────

pub async fn order_detail(
    State(state): State<AppState>,
    Path(order_id): Path<String>,
    Query(flash): Query<FlashQuery>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let order = match clients.com.get_order(&order_id).await {
        Ok(o) => o,
        Err(ClientError::NotFound(_)) => {
            return (StatusCode::NOT_FOUND, format!("Order {order_id} not found")).into_response()
        }
        Err(e) => {
            tracing::error!(error = %e, "csr.order.get_failed");
            return (StatusCode::BAD_GATEWAY, "COM error").into_response();
        }
    };

    // SOM decomposition — best-effort; COM is the page's source of truth.
    let service_orders = match clients.som.list_for_order(&order_id).await {
        Ok(v) => v.as_array().cloned().unwrap_or_default(),
        Err(e) => {
            tracing::warn!(order_id = %order_id, error = %e, "csr.orders.som_fetch_failed");
            Vec::new()
        }
    };

    let mut so_views: Vec<Value> = Vec::with_capacity(service_orders.len());
    for so in &service_orders {
        let so_items_raw = so
            .get("items")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();

        // Resolve each item's target service — best-effort, skip on any failure.
        let mut services: Vec<Value> = Vec::new();
        for item in &so_items_raw {
            let svc_id = field(Some(item), &["target_service_id"]).and_then(Value::as_str);
            let Some(svc_id) = svc_id.filter(|s| !s.is_empty()) else {
                continue;
            };
            if let Ok(s) = clients.som.get_service(svc_id).await {
                services.push(json!({
                    "id": s.get("id").and_then(Value::as_str).unwrap_or("?"),
                    "type": field_str(Some(&s), &["type", "service_type"], "—"),
                    "spec_id": field_str(Some(&s), &["spec_id"], ""),
                    "state": field_str(Some(&s), &["state"], "?"),
                }));
            }
        }

        so_views.push(json!({
            "id": so.get("id").and_then(Value::as_str).unwrap_or("?"),
            "state": field_str(Some(so), &["state"], "?"),
            "started_at": fmt_dt(&field_str(Some(so), &["started_at"], "")),
            "completed_at": fmt_dt(&field_str(Some(so), &["completed_at"], "")),
            // "so_items", not "items" — Jinja resolves attributes before subscripts,
            // and dict.items (the method) would win.
            "so_items": so_items_raw.iter().map(|i| json!({
                "action": field_str(Some(i), &["action"], "—"),
                "spec_id": field_str(Some(i), &["service_spec_id"], "—"),
                "service_id": field_str(Some(i), &["target_service_id"], ""),
            })).collect::<Vec<_>>(),
            "services": services,
        }));
    }

    let items: Vec<Value> = order
        .get("items")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .iter()
        .map(|i| {
            json!({
                "id": i.get("id").and_then(Value::as_str).unwrap_or(""),
                "offering_id": field_str(Some(i), &["offering_id"], "—"),
                "action": field_str(Some(i), &["action"], ""),
                "state": field_str(Some(i), &["state"], ""),
                "price": field_str(Some(i), &["price_amount", "price"], ""),
                "msisdn": field_str(Some(i), &["msisdn"], ""),
            })
        })
        .collect();

    // flatten_order + the three extra detail fields.
    let mut order_view = flatten_order(&order);
    if let Some(obj) = order_view.as_object_mut() {
        obj.insert(
            "notes".to_string(),
            json!(field_str(Some(&order), &["notes"], "")),
        );
        obj.insert(
            "subscription_id".to_string(),
            json!(field_str(Some(&order), &["subscription_id"], "")),
        );
        obj.insert(
            "discount_code".to_string(),
            json!(field_str(Some(&order), &["discount_code"], "")),
        );
    }

    render(
        &state,
        "order_detail.html",
        minijinja::Value::from_serialize(json!({
            "active_page": "orders",
            "model": "(env default)",
            "order": order_view,
            "items": items,
            "service_orders": so_views,
            "flash": flash.flash,
            "err": flash.err.chars().take(300).collect::<String>(),
        })),
    )
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    fn loc(r: Response) -> String {
        r.headers()
            .get("location")
            .unwrap()
            .to_str()
            .unwrap()
            .to_string()
    }

    /// COM errors are worded "COM error", not "CRM error" — the one place the
    /// order screen diverges from the shared `write_result`.
    #[test]
    fn run_words_errors_as_com() {
        let server = ClientError::Server {
            status: 503,
            detail: "down".to_string(),
        };
        assert_eq!(
            loc(run("ORD-1", "x", Err(server))),
            "/orders/ORD-1?err=COM+error+%28503%29"
        );
        let policy = ClientError::Policy(bss_db::PolicyViolation {
            rule: "order.submit.requires_cof".to_string(),
            message: "No card on file.".to_string(),
            context: json!({}),
        });
        assert_eq!(
            loc(run("ORD-1", "x", Err(policy))),
            "/orders/ORD-1?err=No+card+on+file."
        );
        assert_eq!(
            loc(run("ORD-1", "order_submitted", Ok(json!({})))),
            "/orders/ORD-1?flash=order_submitted"
        );
    }
}
