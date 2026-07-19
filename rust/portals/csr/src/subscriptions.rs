//! Subscription detail — balances, services, usage, eSIM (v1.6 cockpit CRM). Port
//! of `bss_csr.routes.subscriptions`.
//!
//! v1.6.1 (operator directive) — lifecycle CRUD is direct: schedule/cancel plan
//! change, renew now, VAS top-up, terminate. Terminate (destructive) and the
//! money-movers (renew, VAS) sit behind the two-step UI confirm (`confirm=yes`);
//! the subscription policy layer stays the server-side gate. The eSIM panel is the
//! v0.10 read-only re-display (NOT a SGP.22 rearm — DECISIONS 2026-04-27).

use axum::extract::{Form, Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use bss_clients::ClientError;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::routes::{back_to, render, CONFIRM_REQUIRED};
use crate::views::{balance_rows, customer_name, field, field_str, fmt_dt};
use crate::AppState;

#[derive(Deserialize, Default)]
pub struct FlashQuery {
    #[serde(default)]
    flash: String,
    #[serde(default)]
    err: String,
}

/// `(payload, ok)` best-effort read; a section down degrades to `None` rather than
/// blanking the whole page.
fn ok_or_none(r: Result<Value, ClientError>) -> Option<Value> {
    match r {
        Ok(v) => Some(v),
        Err(e) => {
            tracing::warn!(error = %e, "csr.subscription.section_failed");
            None
        }
    }
}

fn arr(v: &Option<Value>) -> Vec<Value> {
    v.as_ref()
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

// ── GET /subscriptions/{id} ──────────────────────────────────────────

pub async fn subscription_detail(
    State(state): State<AppState>,
    Path(subscription_id): Path<String>,
    Query(q): Query<FlashQuery>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let sub = match clients.subscription.get(&subscription_id).await {
        Ok(s) => s,
        Err(ClientError::NotFound(_)) => {
            return (
                StatusCode::NOT_FOUND,
                format!("Subscription {subscription_id} not found"),
            )
                .into_response()
        }
        Err(e) => {
            tracing::error!(error = %e, "csr.subscription.get_failed");
            return (StatusCode::BAD_GATEWAY, "Subscription error").into_response();
        }
    };

    let customer_id = field_str(Some(&sub), &["customer_id"], "");
    let offering_id = field_str(Some(&sub), &["offering_id"], "");

    // The seven best-effort sections fan out. The customer/offering fetches only
    // fire when the id is present, matching Python's `… if id else _noop()`.
    let (cust, offering, services, usage, esim, offerings, vas) = tokio::join!(
        async {
            if customer_id.is_empty() {
                None
            } else {
                ok_or_none(clients.crm.get_customer(&customer_id).await)
            }
        },
        async {
            if offering_id.is_empty() {
                None
            } else {
                ok_or_none(clients.catalog.get_offering(&offering_id).await)
            }
        },
        async {
            ok_or_none(
                clients
                    .som
                    .list_services_for_subscription(&subscription_id)
                    .await,
            )
        },
        async {
            ok_or_none(
                clients
                    .mediation
                    .list_usage(Some(&subscription_id), None, None, None, 15)
                    .await,
            )
        },
        async {
            ok_or_none(
                clients
                    .subscription
                    .get_esim_activation(&subscription_id)
                    .await,
            )
        },
        async {
            ok_or_none(
                clients
                    .catalog
                    .list_active_offerings(&bss_clock::now().to_rfc3339())
                    .await,
            )
        },
        async { ok_or_none(clients.catalog.list_vas().await) },
    );

    let usage_views: Vec<Value> = arr(&usage)
        .iter()
        .map(|u| {
            json!({
                "at": fmt_dt(&field_str(Some(u), &["event_time", "occurred_at"], "")),
                "type": field_str(Some(u), &["event_type", "type"], "—"),
                "quantity": format!(
                    "{} {}",
                    field_str(Some(u), &["quantity"], "?"),
                    field_str(Some(u), &["unit"], ""),
                ).trim().to_string(),
                "roaming": field(Some(u), &["roaming_indicator"]).and_then(Value::as_bool).unwrap_or(false),
            })
        })
        .collect();

    let service_views: Vec<Value> = arr(&services)
        .iter()
        .map(|s| {
            json!({
                "id": s.get("id").and_then(Value::as_str).unwrap_or("?"),
                "type": field_str(Some(s), &["type", "service_type"], "—"),
                "spec_id": field_str(Some(s), &["spec_id"], ""),
                "state": field_str(Some(s), &["state"], "?"),
            })
        })
        .collect();

    // price — "CUR amount" plus an optional "promo CODE", joined with " · ".
    let mut price_bits: Vec<String> = Vec::new();
    if let Some(amount) = field(Some(&sub), &["effective_amount", "price_amount"]) {
        if !amount.is_null() {
            let amt = match amount {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            };
            price_bits.push(format!(
                "{} {}",
                field_str(Some(&sub), &["price_currency"], "SGD"),
                amt
            ));
        }
    }
    let promo = field_str(Some(&sub), &["promo_code"], "");
    if !promo.is_empty() {
        price_bits.push(format!("promo {promo}"));
    }
    let price = if price_bits.is_empty() {
        "—".to_string()
    } else {
        price_bits.join(" · ")
    };

    let plan_options: Vec<String> = arr(&offerings)
        .iter()
        .filter(|o| {
            o.get("isBundle").and_then(Value::as_bool).unwrap_or(true)
                && o.get("id").and_then(Value::as_str) != Some(offering_id.as_str())
        })
        .map(|o| {
            o.get("id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string()
        })
        .collect();

    let vas_options: Vec<Value> = arr(&vas)
        .iter()
        .map(|v| {
            json!({
                "id": v.get("id").and_then(Value::as_str).unwrap_or(""),
                "name": v.get("name").and_then(Value::as_str).unwrap_or(""),
                "price": format!(
                    "{} {}",
                    v.get("currency").and_then(Value::as_str).unwrap_or("SGD"),
                    v.get("priceAmount").map(scalar).unwrap_or_else(|| "?".to_string()),
                ),
            })
        })
        .collect();

    let esim_view = esim.as_ref().map(|e| {
        json!({
            "iccid": e.get("iccid").and_then(Value::as_str).unwrap_or(""),
            "activation_code": field_str(Some(e), &["activation_code"], ""),
        })
    });

    let ctx = json!({
        "active_page": "customers",
        "model": "(env default)",
        "sub": {
            "id": sub.get("id").and_then(Value::as_str).unwrap_or(&subscription_id),
            "state": field_str(Some(&sub), &["state"], "?"),
            "state_reason": field_str(Some(&sub), &["state_reason"], ""),
            "msisdn": sub.get("msisdn").and_then(Value::as_str).unwrap_or("—"),
            "iccid": sub.get("iccid").and_then(Value::as_str).unwrap_or(""),
            "customer_id": customer_id,
            "customer_name": customer_name(cust.as_ref()),
            "offering_id": offering_id,
            "offering_name": offering.as_ref().and_then(|o| o.get("name")).and_then(Value::as_str).unwrap_or(""),
            "price": price,
            "activated_at": fmt_dt(&field_str(Some(&sub), &["activated_at"], "")),
            "period_end": fmt_dt(&field_str(Some(&sub), &["current_period_end"], "")),
            "next_renewal": fmt_dt(&field_str(Some(&sub), &["next_renewal_at"], "")),
            "pending_offering_id": field_str(Some(&sub), &["pending_offering_id"], ""),
            "pending_effective_at": fmt_dt(&field_str(Some(&sub), &["pending_effective_at"], "")),
        },
        "balances": balance_rows(sub.get("balances")),
        "services": service_views,
        "usage": usage_views,
        "esim": esim_view,
        "plan_options": plan_options,
        "vas_options": vas_options,
        "flash": q.flash,
        "err": q.err.chars().take(300).collect::<String>(),
    });

    render(
        &state,
        "subscription_detail.html",
        minijinja::Value::from_serialize(ctx),
    )
}

/// A JSON scalar as Python's f-string renders it (int without `.0`, string bare).
fn scalar(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Number(n) => n
            .as_i64()
            .map(|i| i.to_string())
            .unwrap_or_else(|| n.to_string()),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

// ── writes ───────────────────────────────────────────────────────────

fn back_to_sub(subscription_id: &str, flash: &str, err: &str) -> Response {
    back_to(&format!("/subscriptions/{subscription_id}"), flash, err)
}

/// `_write` — subscription errors are worded "Subscription error (…)".
fn run(subscription_id: &str, action: &str, r: Result<Value, ClientError>) -> Response {
    match r {
        Ok(_) => back_to_sub(subscription_id, action, ""),
        Err(ClientError::Policy(p)) => back_to_sub(subscription_id, "", &p.message),
        Err(e) => back_to_sub(
            subscription_id,
            "",
            &format!("Subscription error ({})", e.status_code()),
        ),
    }
}

#[derive(Deserialize)]
pub struct PlanChangeForm {
    new_offering_id: String,
}

/// `POST /subscriptions/{id}/plan-change`.
pub async fn schedule_plan_change(
    State(state): State<AppState>,
    Path(subscription_id): Path<String>,
    Form(form): Form<PlanChangeForm>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients
        .subscription
        .schedule_plan_change(&subscription_id, form.new_offering_id.trim())
        .await;
    run(&subscription_id, "plan_change_scheduled", r)
}

/// `POST /subscriptions/{id}/plan-change/cancel` (no confirm — clears pending).
pub async fn cancel_plan_change(
    State(state): State<AppState>,
    Path(subscription_id): Path<String>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients
        .subscription
        .cancel_plan_change(&subscription_id)
        .await;
    run(&subscription_id, "plan_change_cancelled", r)
}

#[derive(Deserialize, Default)]
pub struct ConfirmForm {
    #[serde(default)]
    confirm: String,
}

/// `POST /subscriptions/{id}/renew` — **confirm-gated** (charges COF).
pub async fn renew_now(
    State(state): State<AppState>,
    Path(subscription_id): Path<String>,
    Form(form): Form<ConfirmForm>,
) -> Response {
    if form.confirm != "yes" {
        return back_to_sub(&subscription_id, "", CONFIRM_REQUIRED);
    }
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients.subscription.renew(&subscription_id).await;
    run(&subscription_id, "renewed", r)
}

#[derive(Deserialize)]
pub struct VasForm {
    vas_offering_id: String,
    #[serde(default)]
    confirm: String,
}

/// `POST /subscriptions/{id}/vas` — **confirm-gated** (charges COF).
pub async fn purchase_vas(
    State(state): State<AppState>,
    Path(subscription_id): Path<String>,
    Form(form): Form<VasForm>,
) -> Response {
    if form.confirm != "yes" {
        return back_to_sub(&subscription_id, "", CONFIRM_REQUIRED);
    }
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients
        .subscription
        .purchase_vas(&subscription_id, form.vas_offering_id.trim())
        .await;
    run(&subscription_id, "vas_purchased", r)
}

#[derive(Deserialize, Default)]
pub struct TerminateForm {
    #[serde(default)]
    reason: String,
    #[serde(default = "yes")]
    release_inventory: String,
    #[serde(default)]
    confirm: String,
}

fn yes() -> String {
    "yes".to_string()
}

/// `POST /subscriptions/{id}/terminate` — **confirm-gated** (destructive).
pub async fn terminate(
    State(state): State<AppState>,
    Path(subscription_id): Path<String>,
    Form(form): Form<TerminateForm>,
) -> Response {
    if form.confirm != "yes" {
        return back_to_sub(&subscription_id, "", CONFIRM_REQUIRED);
    }
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let reason = form.reason.trim();
    // `terminate_with_reason` reproduces Python's None-body logic: reason=None +
    // release=true sends no body, so the server's "customer_requested" default and
    // inventory release both apply.
    let r = clients
        .subscription
        .terminate_with_reason(
            &subscription_id,
            if reason.is_empty() {
                None
            } else {
                Some(reason)
            },
            form.release_inventory == "yes",
        )
        .await;
    run(&subscription_id, "terminated", r)
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

    #[test]
    fn run_words_errors_as_subscription() {
        let server = ClientError::Server {
            status: 502,
            detail: "down".to_string(),
        };
        assert_eq!(
            loc(run("SUB-7", "x", Err(server))),
            "/subscriptions/SUB-7?err=Subscription+error+%28502%29"
        );
        assert_eq!(
            loc(run("SUB-7", "renewed", Ok(json!({})))),
            "/subscriptions/SUB-7?flash=renewed"
        );
    }

    #[test]
    fn scalar_renders_ints_bare() {
        assert_eq!(scalar(&json!(10)), "10");
        assert_eq!(scalar(&json!("1")), "1");
    }
}
