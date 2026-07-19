//! Subscription-write account surface: plan change (schedule/cancel), cancel
//! (terminate), and top-up (VAS purchase). Port of `bss_self_serve.routes.{plan_change,cancel,top_up}`.
//!
//! Every mutating POST is `requires_linked_customer` + step-up-gated, with a
//! server-side ownership check (not-found == not-yours forensic posture). One
//! `bss-clients` write per route; `portal_action` on success + failure.

use axum::extract::{Path, Query, RawForm, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Redirect, Response};
use axum::Extension;
use minijinja::context;
use serde::Deserialize;
use serde_json::{json, Value};

use bss_clients::ClientError;

use crate::deps::require_linked_customer;
use crate::error_messages::{is_known, render as render_rule};
use crate::middleware::PortalSession;
use crate::profile::{audit, field, parse_form, user_agent};
use crate::routes::render;
use crate::stepup::check_step_up;
use crate::templating::request_ctx;
use crate::AppState;

const OWNERSHIP_RULE: &str = "policy.ownership.subscription_not_owned";

/// Ownership: the subscription must belong to `customer_id`, else `None`.
async fn check_ownership(state: &AppState, sub_id: &str, customer_id: &str) -> Option<Value> {
    let clients = state.clients.as_ref()?;
    let sub = clients.subscription.get(sub_id).await.ok()?;
    if sub.get("customerId").and_then(Value::as_str) == Some(customer_id) {
        Some(sub)
    } else {
        None
    }
}

fn identity_id(portal: &PortalSession) -> String {
    portal
        .identity
        .as_ref()
        .map(|i| i.id.clone())
        .unwrap_or_default()
}

// ── query payloads ───────────────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct SubQuery {
    subscription: String,
    #[serde(default)]
    context: Option<String>,
}

#[derive(Deserialize)]
pub struct VasSuccessQuery {
    subscription: String,
    vas: String,
}

#[derive(Deserialize)]
pub struct ScheduledQuery {
    subscription: String,
    new_offering: String,
    #[serde(default)]
    effective_at: String,
}

// ── plan change: (currency, amount) + card builder ───────────────────────────

fn format_price(o: &Value) -> (String, String) {
    let prices = o.get("productOfferingPrice").and_then(Value::as_array);
    let Some(first) = prices.and_then(|a| a.first()) else {
        return ("SGD".to_string(), String::new());
    };
    let price = first.get("price");
    let inner = price
        .and_then(|p| p.get("taxIncludedAmount"))
        .or_else(|| price.and_then(|p| p.get("amount")));
    let currency = inner
        .and_then(|i| i.get("unit").or_else(|| i.get("currency")))
        .and_then(Value::as_str)
        .unwrap_or("SGD")
        .to_string();
    let amount = inner
        .and_then(|i| i.get("value"))
        .map(|v| match v {
            Value::String(s) => s.clone(),
            Value::Null => String::new(),
            other => other.to_string(),
        })
        .unwrap_or_default();
    (currency, amount)
}

fn plan_cards(offerings: &[Value], current: Option<&str>, pending: Option<&str>) -> Vec<Value> {
    offerings
        .iter()
        .map(|o| {
            let (currency, amount) = format_price(o);
            let id = o.get("id").and_then(Value::as_str).unwrap_or("");
            json!({
                "id": id,
                "name": o.get("name").and_then(Value::as_str).unwrap_or(id),
                "currency": currency,
                "amount": amount,
                "is_current": Some(id) == current,
                "is_pending": Some(id) == pending,
                "allowances": o.get("bundleAllowance").cloned().unwrap_or(json!([])),
            })
        })
        .collect()
}

async fn render_plan_form(
    state: &AppState,
    portal: &PortalSession,
    sub: &Value,
    error: Option<&str>,
    status: StatusCode,
) -> Response {
    let offerings = match &state.clients {
        Some(c) => c
            .catalog
            .list_active_offerings(&bss_clock::now().to_rfc3339())
            .await
            .ok()
            .and_then(|v| v.as_array().cloned())
            .unwrap_or_default(),
        None => Vec::new(),
    };
    let current = sub.get("offeringId").and_then(Value::as_str);
    let pending = sub.get("pendingOfferingId").and_then(Value::as_str);
    let cards = plan_cards(&offerings, current, pending);
    let mut resp = render(
        state,
        "plan_change.html",
        context! {
            subscription => sub.clone(),
            current_offering_id => current,
            pending_offering_id => pending,
            pending_effective_at => sub.get("pendingEffectiveAt").cloned().unwrap_or(Value::Null),
            next_renewal_at => sub.get("nextRenewalAt").cloned().unwrap_or(Value::Null),
            cards => minijinja::Value::from_serialize(&cards),
            error => error,
            request => request_ctx("/plan/change", portal.identity_email()),
        },
    );
    *resp.status_mut() = status;
    resp
}

fn plan_forbidden(state: &AppState, portal: &PortalSession, template: &str) -> Response {
    let mut resp = render(
        state,
        template,
        context! {
            customer_facing => render_rule(OWNERSHIP_RULE),
            request => request_ctx("/", portal.identity_email()),
        },
    );
    *resp.status_mut() = StatusCode::FORBIDDEN;
    resp
}

// ── GET/POST /plan/change ────────────────────────────────────────────────────

pub async fn plan_change_form(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Query(q): Query<SubQuery>,
) -> Response {
    let customer_id = match require_linked_customer(&portal, "/") {
        Ok(c) => c,
        Err(r) => return r,
    };
    match check_ownership(&state, &q.subscription, &customer_id).await {
        Some(sub) => render_plan_form(&state, &portal, &sub, None, StatusCode::OK).await,
        None => plan_forbidden(&state, &portal, "plan_change_forbidden.html"),
    }
}

pub async fn plan_change_submit(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Query(q): Query<SubQuery>,
    headers: HeaderMap,
    RawForm(body): RawForm,
) -> Response {
    let form = parse_form(&body);
    let customer_id = match require_linked_customer(&portal, "/") {
        Ok(c) => c,
        Err(r) => return r,
    };
    let target = format!("/plan/change?subscription={}", q.subscription);
    if let Err(r) = check_step_up(
        &state,
        &portal,
        "plan_change_schedule",
        &headers,
        &form,
        &target,
    )
    .await
    {
        return r;
    }
    let iid = identity_id(&portal);
    let ua = user_agent(&headers);
    let new_offering = field(&form, "new_offering_id").unwrap_or("").to_string();

    let Some(sub) = check_ownership(&state, &q.subscription, &customer_id).await else {
        audit(
            &state,
            &customer_id,
            &iid,
            "plan_change_schedule",
            "/plan/change",
            false,
            Some(OWNERSHIP_RULE),
            true,
            ua.as_deref(),
        )
        .await;
        return plan_forbidden(&state, &portal, "plan_change_forbidden.html");
    };
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };
    match clients
        .subscription
        .schedule_plan_change(&q.subscription, &new_offering)
        .await
    {
        Ok(result) => {
            audit(
                &state,
                &customer_id,
                &iid,
                "plan_change_schedule",
                "/plan/change",
                true,
                None,
                true,
                ua.as_deref(),
            )
            .await;
            let effective_at = result
                .get("pendingEffectiveAt")
                .and_then(Value::as_str)
                .unwrap_or("");
            Redirect::to(&format!(
                "/plan/change/scheduled?subscription={}&new_offering={new_offering}&effective_at={effective_at}",
                q.subscription
            ))
            .into_response()
        }
        Err(ClientError::Policy(pv)) => {
            audit(
                &state,
                &customer_id,
                &iid,
                "plan_change_schedule",
                "/plan/change",
                false,
                Some(&pv.rule),
                true,
                ua.as_deref(),
            )
            .await;
            if !is_known(&pv.rule) {
                tracing::info!(rule = %pv.rule, "portal.plan_change.unknown_policy_rule");
            }
            render_plan_form(
                &state,
                &portal,
                &sub,
                Some(render_rule(&pv.rule)),
                StatusCode::UNPROCESSABLE_ENTITY,
            )
            .await
        }
        Err(e) => {
            tracing::error!(error = %e, "portal.plan_change.schedule_failed");
            (StatusCode::INTERNAL_SERVER_ERROR, "Failed").into_response()
        }
    }
}

pub async fn plan_change_scheduled(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Query(q): Query<ScheduledQuery>,
) -> Response {
    let customer_id = match require_linked_customer(&portal, "/") {
        Ok(c) => c,
        Err(r) => return r,
    };
    let Some(sub) = check_ownership(&state, &q.subscription, &customer_id).await else {
        return plan_forbidden(&state, &portal, "plan_change_forbidden.html");
    };
    let new_name = match &state.clients {
        Some(c) => c
            .catalog
            .list_offerings()
            .await
            .ok()
            .and_then(|v| v.as_array().cloned())
            .and_then(|arr| {
                arr.iter()
                    .find(|o| o.get("id").and_then(Value::as_str) == Some(q.new_offering.as_str()))
                    .and_then(|o| o.get("name").and_then(Value::as_str).map(str::to_string))
            })
            .unwrap_or_else(|| q.new_offering.clone()),
        None => q.new_offering.clone(),
    };
    render(
        &state,
        "plan_change_scheduled.html",
        context! {
            subscription => sub,
            new_offering_id => q.new_offering,
            new_offering_name => new_name,
            effective_at => q.effective_at,
            request => request_ctx("/plan/change", portal.identity_email()),
        },
    )
}

pub async fn plan_change_cancel(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    headers: HeaderMap,
    RawForm(body): RawForm,
) -> Response {
    let form = parse_form(&body);
    let customer_id = match require_linked_customer(&portal, "/") {
        Ok(c) => c,
        Err(r) => return r,
    };
    if let Err(r) = check_step_up(
        &state,
        &portal,
        "plan_change_cancel",
        &headers,
        &form,
        "/plan/change/cancel",
    )
    .await
    {
        return r;
    }
    let iid = identity_id(&portal);
    let ua = user_agent(&headers);
    let sub_id = field(&form, "subscription_id").unwrap_or("").to_string();

    if check_ownership(&state, &sub_id, &customer_id)
        .await
        .is_none()
    {
        audit(
            &state,
            &customer_id,
            &iid,
            "plan_change_cancel",
            "/plan/change/cancel",
            false,
            Some(OWNERSHIP_RULE),
            true,
            ua.as_deref(),
        )
        .await;
        return plan_forbidden(&state, &portal, "plan_change_forbidden.html");
    }
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };
    match clients.subscription.cancel_plan_change(&sub_id).await {
        Ok(_) => {
            audit(
                &state,
                &customer_id,
                &iid,
                "plan_change_cancel",
                "/plan/change/cancel",
                true,
                None,
                true,
                ua.as_deref(),
            )
            .await;
            Redirect::to("/?flash=plan_change_cancelled").into_response()
        }
        Err(ClientError::Policy(pv)) => {
            audit(
                &state,
                &customer_id,
                &iid,
                "plan_change_cancel",
                "/plan/change/cancel",
                false,
                Some(&pv.rule),
                true,
                ua.as_deref(),
            )
            .await;
            Redirect::to(&format!(
                "/?flash=plan_change_cancel_failed&rule={}",
                pv.rule
            ))
            .into_response()
        }
        Err(e) => {
            tracing::error!(error = %e, "portal.plan_change.cancel_failed");
            (StatusCode::INTERNAL_SERVER_ERROR, "Failed").into_response()
        }
    }
}

// ── cancel (terminate) ───────────────────────────────────────────────────────

fn losses_for(sub: &Value, balances: &[Value]) -> Value {
    let iccid = sub.get("iccid").and_then(Value::as_str).unwrap_or("");
    let iccid_last4 = if iccid.len() >= 4 {
        &iccid[iccid.len() - 4..]
    } else {
        "----"
    };
    let bals: Vec<Value> = balances
        .iter()
        .map(|b| {
            let atype = b
                .get("allowanceType")
                .and_then(Value::as_str)
                .unwrap_or("?");
            let mut label = atype.to_string();
            if let Some(c) = label.get_mut(0..1) {
                c.make_ascii_uppercase();
            }
            json!({
                "label": label,
                "remaining": b.get("remaining").cloned().unwrap_or(json!(0)),
                "total": b.get("total").cloned().unwrap_or(json!(0)),
                "unit": b.get("unit").and_then(Value::as_str).unwrap_or(""),
                "unlimited": b.get("total").and_then(Value::as_i64).unwrap_or(0) < 0,
            })
        })
        .collect();
    json!({
        "current_period_end": sub.get("currentPeriodEnd").cloned().unwrap_or(Value::Null),
        "msisdn": sub.get("msisdn").and_then(Value::as_str),
        "iccid_last4": iccid_last4,
        "balances": bals,
    })
}

async fn balances_of(state: &AppState, sub_id: &str) -> Vec<Value> {
    match &state.clients {
        Some(c) => c
            .subscription
            .get_balance(sub_id)
            .await
            .ok()
            .and_then(|v| v.as_array().cloned())
            .unwrap_or_default(),
        None => Vec::new(),
    }
}

pub async fn cancel_confirm(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Path(sub_id): Path<String>,
) -> Response {
    let customer_id = match require_linked_customer(&portal, "/") {
        Ok(c) => c,
        Err(r) => return r,
    };
    let Some(sub) = check_ownership(&state, &sub_id, &customer_id).await else {
        return plan_forbidden(&state, &portal, "cancel_forbidden.html");
    };
    if sub.get("state").and_then(Value::as_str) == Some("terminated") {
        return render(
            &state,
            "cancel_already_terminated.html",
            context! { subscription => sub, request => request_ctx("/", portal.identity_email()) },
        );
    }
    let balances = balances_of(&state, &sub_id).await;
    render(
        &state,
        "cancel_confirm.html",
        context! {
            subscription => sub.clone(),
            losses => losses_for(&sub, &balances),
            error => Option::<String>::None,
            request => request_ctx("/", portal.identity_email()),
        },
    )
}

pub async fn cancel_submit(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Path(sub_id): Path<String>,
    headers: HeaderMap,
    // `Bytes`, not `RawForm`: the cancel POST is legitimately bodyless (the step-up
    // grant rides in a cookie, no form fields), and `RawForm` 415s a request with no
    // `Content-Type`. The Python route tolerated it; `Bytes` reads any/empty body.
    body: axum::body::Bytes,
) -> Response {
    let form = parse_form(&body);
    let customer_id = match require_linked_customer(&portal, "/") {
        Ok(c) => c,
        Err(r) => return r,
    };
    let route = format!("/subscription/{sub_id}/cancel");
    if let Err(r) = check_step_up(
        &state,
        &portal,
        "subscription_terminate",
        &headers,
        &form,
        &route,
    )
    .await
    {
        return r;
    }
    let iid = identity_id(&portal);
    let ua = user_agent(&headers);

    let Some(sub) = check_ownership(&state, &sub_id, &customer_id).await else {
        audit(
            &state,
            &customer_id,
            &iid,
            "subscription_terminate",
            &route,
            false,
            Some(OWNERSHIP_RULE),
            true,
            ua.as_deref(),
        )
        .await;
        return plan_forbidden(&state, &portal, "cancel_forbidden.html");
    };
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };
    match clients
        .subscription
        .terminate_with_reason(&sub_id, Some("customer_requested"), true)
        .await
    {
        Ok(_) => {
            audit(
                &state,
                &customer_id,
                &iid,
                "subscription_terminate",
                &route,
                true,
                None,
                true,
                ua.as_deref(),
            )
            .await;
            Redirect::to(&format!("/subscription/{sub_id}/cancelled")).into_response()
        }
        Err(ClientError::Policy(pv)) => {
            audit(
                &state,
                &customer_id,
                &iid,
                "subscription_terminate",
                &route,
                false,
                Some(&pv.rule),
                true,
                ua.as_deref(),
            )
            .await;
            if !is_known(&pv.rule) {
                tracing::info!(rule = %pv.rule, "portal.cancel.unknown_policy_rule");
            }
            let balances = balances_of(&state, &sub_id).await;
            let mut resp = render(
                &state,
                "cancel_confirm.html",
                context! {
                    subscription => sub.clone(),
                    losses => losses_for(&sub, &balances),
                    error => render_rule(&pv.rule),
                    request => request_ctx("/", portal.identity_email()),
                },
            );
            *resp.status_mut() = StatusCode::UNPROCESSABLE_ENTITY;
            resp
        }
        Err(e) => {
            tracing::error!(error = %e, "portal.cancel.terminate_failed");
            (StatusCode::INTERNAL_SERVER_ERROR, "Failed").into_response()
        }
    }
}

pub async fn cancel_success(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Path(sub_id): Path<String>,
) -> Response {
    let customer_id = match require_linked_customer(&portal, "/") {
        Ok(c) => c,
        Err(r) => return r,
    };
    match check_ownership(&state, &sub_id, &customer_id).await {
        Some(sub) => render(
            &state,
            "cancel_success.html",
            context! { subscription => sub, request => request_ctx("/", portal.identity_email()) },
        ),
        None => plan_forbidden(&state, &portal, "cancel_forbidden.html"),
    }
}

// ── top-up (VAS purchase) ────────────────────────────────────────────────────

async fn vas_offerings(state: &AppState) -> Vec<Value> {
    match &state.clients {
        Some(c) => c
            .catalog
            .list_vas()
            .await
            .ok()
            .and_then(|v| v.as_array().cloned())
            .unwrap_or_default(),
        None => Vec::new(),
    }
}

pub async fn top_up_form(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Query(q): Query<SubQuery>,
) -> Response {
    let customer_id = match require_linked_customer(&portal, "/") {
        Ok(c) => c,
        Err(r) => return r,
    };
    let Some(sub) = check_ownership(&state, &q.subscription, &customer_id).await else {
        return plan_forbidden(&state, &portal, "top_up_forbidden.html");
    };
    let vas = vas_offerings(&state).await;
    let pre_select = if q.context.as_deref() == Some("blocked") {
        vas.iter()
            .find(|v| v.get("allowanceType").and_then(Value::as_str) == Some("data"))
            .and_then(|v| v.get("id").and_then(Value::as_str))
            .map(str::to_string)
    } else {
        None
    };
    render(
        &state,
        "top_up.html",
        context! {
            subscription => sub,
            vas_offerings => minijinja::Value::from_serialize(&vas),
            pre_select_id => pre_select,
            context => q.context,
            error => Option::<String>::None,
            request => request_ctx("/top-up", portal.identity_email()),
        },
    )
}

pub async fn top_up_submit(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Query(q): Query<SubQuery>,
    headers: HeaderMap,
    RawForm(body): RawForm,
) -> Response {
    let form = parse_form(&body);
    let customer_id = match require_linked_customer(&portal, "/") {
        Ok(c) => c,
        Err(r) => return r,
    };
    let target = format!("/top-up?subscription={}", q.subscription);
    if let Err(r) = check_step_up(&state, &portal, "vas_purchase", &headers, &form, &target).await {
        return r;
    }
    let iid = identity_id(&portal);
    let ua = user_agent(&headers);
    let vas_id = field(&form, "vas_offering_id").unwrap_or("").to_string();

    let Some(sub) = check_ownership(&state, &q.subscription, &customer_id).await else {
        audit(
            &state,
            &customer_id,
            &iid,
            "vas_purchase",
            "/top-up",
            false,
            Some(OWNERSHIP_RULE),
            true,
            ua.as_deref(),
        )
        .await;
        return plan_forbidden(&state, &portal, "top_up_forbidden.html");
    };
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };
    match clients
        .subscription
        .purchase_vas(&q.subscription, &vas_id)
        .await
    {
        Ok(_) => {
            audit(
                &state,
                &customer_id,
                &iid,
                "vas_purchase",
                "/top-up",
                true,
                None,
                true,
                ua.as_deref(),
            )
            .await;
            Redirect::to(&format!(
                "/top-up/success?subscription={}&vas={vas_id}",
                q.subscription
            ))
            .into_response()
        }
        Err(ClientError::Policy(pv)) => {
            audit(
                &state,
                &customer_id,
                &iid,
                "vas_purchase",
                "/top-up",
                false,
                Some(&pv.rule),
                true,
                ua.as_deref(),
            )
            .await;
            if !is_known(&pv.rule) {
                tracing::info!(rule = %pv.rule, "portal.top_up.unknown_policy_rule");
            }
            let vas = vas_offerings(&state).await;
            let mut resp = render(
                &state,
                "top_up.html",
                context! {
                    subscription => sub,
                    vas_offerings => minijinja::Value::from_serialize(&vas),
                    pre_select_id => vas_id,
                    context => Option::<String>::None,
                    error => render_rule(&pv.rule),
                    request => request_ctx("/top-up", portal.identity_email()),
                },
            );
            *resp.status_mut() = StatusCode::UNPROCESSABLE_ENTITY;
            resp
        }
        Err(e) => {
            tracing::error!(error = %e, "portal.top_up.purchase_failed");
            (StatusCode::INTERNAL_SERVER_ERROR, "Failed").into_response()
        }
    }
}

pub async fn top_up_success(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Query(q): Query<VasSuccessQuery>,
) -> Response {
    let customer_id = match require_linked_customer(&portal, "/") {
        Ok(c) => c,
        Err(r) => return r,
    };
    let Some(sub) = check_ownership(&state, &q.subscription, &customer_id).await else {
        return plan_forbidden(&state, &portal, "top_up_forbidden.html");
    };
    let balances = balances_of(&state, &q.subscription).await;
    let vas = vas_offerings(&state).await;
    let vas_offering = vas
        .iter()
        .find(|v| v.get("id").and_then(Value::as_str) == Some(q.vas.as_str()))
        .cloned()
        .unwrap_or_else(|| json!({ "id": q.vas, "name": q.vas }));
    render(
        &state,
        "top_up_success.html",
        context! {
            subscription => sub,
            vas_offering => vas_offering,
            balances => minijinja::Value::from_serialize(&balances),
            request => request_ctx("/top-up", portal.identity_email()),
        },
    )
}
