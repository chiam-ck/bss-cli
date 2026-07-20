//! `/payment-methods` — list + add (mock card form) + remove + set-default. Port
//! of `bss_self_serve.routes.payment_methods` (the mock-mode paths).
//!
//! Add/remove/set-default are step-up-gated. The mock add tokenizes the PAN
//! server-side (v0.10 behaviour); the Stripe Checkout add flow
//! (`checkout-init`/`checkout-return`) is deferred (prod-only). One `bss-clients`
//! write per route; `portal_action` on success + failure.

use axum::extract::{Path, Query, RawForm, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Redirect, Response};
use axum::Extension;
use minijinja::context;
use serde_json::Value;

use bss_clients::ClientError;

use crate::deps::require_linked_customer;
use crate::error_messages::{is_known, render as render_rule};
use crate::middleware::PortalSession;
use crate::profile::{
    audit as audit_action, field as pick_field, parse_form as parse_form_pairs, user_agent as ua_of,
};
use crate::routes::render;
use crate::signup::local_tokenize;
use crate::stepup::check_step_up;
use crate::templating::request_ctx;
use crate::AppState;

const OWNERSHIP_RULE: &str = "policy.payment.method.unknown";

fn add_template(provider: &str) -> &'static str {
    if provider == "stripe" {
        "payment_methods_add.html"
    } else {
        "payment_methods_add_mock.html"
    }
}

// ── GET /payment-methods ─────────────────────────────────────────────────────

pub async fn list_methods(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Query(q): Query<FlashQuery>,
) -> Response {
    let customer_id = match require_linked_customer(&portal, "/payment-methods") {
        Ok(c) => c,
        Err(r) => return r,
    };
    render_list(
        &state,
        &portal,
        &customer_id,
        None,
        q.flash.as_deref(),
        StatusCode::OK,
    )
    .await
}

/// `?flash=<code>` post-redirect-get confirmation marker (see the
/// `Redirect::to("/payment-methods?flash=...")` writes in this module).
#[derive(serde::Deserialize)]
pub struct FlashQuery {
    #[serde(default)]
    flash: Option<String>,
}

async fn render_list(
    state: &AppState,
    portal: &PortalSession,
    customer_id: &str,
    error: Option<&str>,
    flash: Option<&str>,
    status: StatusCode,
) -> Response {
    let methods = match &state.clients {
        Some(c) => c
            .payment
            .list_methods(customer_id)
            .await
            .unwrap_or(Value::Array(Vec::new())),
        None => Value::Array(Vec::new()),
    };
    let mut resp = render(
        state,
        "payment_methods.html",
        context! {
            methods => methods,
            error => error,
            flash => flash,
            request => request_ctx("/payment-methods", portal.identity_email()),
        },
    );
    *resp.status_mut() = status;
    resp
}

// ── GET /payment-methods/add ─────────────────────────────────────────────────

pub async fn add_method_form(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
) -> Response {
    if let Err(r) = require_linked_customer(&portal, "/payment-methods") {
        return r;
    }
    let provider = state.settings.payment_provider.clone();
    render(
        &state,
        add_template(&provider),
        context! {
            error => Option::<String>::None,
            fields => serde_json::json!({}),
            payment_provider => provider,
            request => request_ctx("/payment-methods", portal.identity_email()),
        },
    )
}

// ── POST /payment-methods/add — mock card form ───────────────────────────────

pub async fn add_method(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    headers: HeaderMap,
    RawForm(body): RawForm,
) -> Response {
    let form = parse_form_pairs(&body);
    let customer_id = match require_linked_customer(&portal, "/payment-methods") {
        Ok(c) => c,
        Err(r) => return r,
    };
    if let Err(r) = check_step_up(
        &state,
        &portal,
        "payment_method_add",
        &headers,
        &form,
        "/payment-methods/add",
    )
    .await
    {
        return r;
    }
    let identity_id = portal
        .identity
        .as_ref()
        .map(|i| i.id.clone())
        .unwrap_or_default();
    let ua = ua_of(&headers);
    let provider = state.settings.payment_provider.clone();

    // Stripe-mode → the Checkout init flow (deferred; sandbox runs mock).
    if provider == "stripe" {
        return Redirect::to("/payment-methods/add/checkout-init").into_response();
    }

    let card_number = pick_field(&form, "card_number").unwrap_or("");
    let exp_month = pick_field(&form, "exp_month").and_then(|v| v.parse::<i64>().ok());
    let exp_year = pick_field(&form, "exp_year").and_then(|v| v.parse::<i64>().ok());
    let cvv = pick_field(&form, "cvv").unwrap_or("");
    let holder_name = pick_field(&form, "holder_name").unwrap_or("");

    let render_add_err = |state: &AppState, msg: &str| -> Response {
        let mut r = render(
            state,
            add_template(&provider),
            context! {
                error => msg,
                fields => serde_json::json!({"exp_month": exp_month, "exp_year": exp_year}),
                payment_provider => provider.clone(),
                request => request_ctx("/payment-methods", portal.identity_email()),
            },
        );
        *r.status_mut() = StatusCode::UNPROCESSABLE_ENTITY;
        r
    };

    // Field validation (mirrors the Python guard).
    let em_ok = exp_month.map(|m| (1..=12).contains(&m)).unwrap_or(false);
    let ey_ok = exp_year
        .map(|y| (2026..=2099).contains(&y))
        .unwrap_or(false);
    if card_number.len() < 12
        || !em_ok
        || !ey_ok
        || !(3..=4).contains(&cvv.len())
        || holder_name.is_empty()
    {
        return render_add_err(&state, "Please fill in every field with a valid value.");
    }

    let tok = match local_tokenize(card_number) {
        Ok(t) => t,
        Err(()) => {
            audit_action(
                &state,
                &customer_id,
                &identity_id,
                "payment_method_add",
                "/payment-methods/add",
                false,
                Some("policy.payment.method.invalid_card"),
                true,
                ua.as_deref(),
            )
            .await;
            return render_add_err(
                &state,
                "That card number doesn't look right. Check the digits.",
            );
        }
    };

    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };
    match clients
        .payment
        .create_payment_method(
            &customer_id,
            &tok.card_token,
            &tok.last4,
            &tok.brand,
            exp_month.unwrap_or(12),
            exp_year.unwrap_or(2030),
        )
        .await
    {
        Ok(_) => {
            audit_action(
                &state,
                &customer_id,
                &identity_id,
                "payment_method_add",
                "/payment-methods/add",
                true,
                None,
                true,
                ua.as_deref(),
            )
            .await;
            Redirect::to("/payment-methods?flash=added").into_response()
        }
        Err(ClientError::Policy(pv)) => {
            audit_action(
                &state,
                &customer_id,
                &identity_id,
                "payment_method_add",
                "/payment-methods/add",
                false,
                Some(&pv.rule),
                true,
                ua.as_deref(),
            )
            .await;
            if !is_known(&pv.rule) {
                tracing::info!(rule = %pv.rule, "portal.payment_methods.unknown_policy_rule");
            }
            render_add_err(&state, render_rule(&pv.rule))
        }
        Err(e) => {
            tracing::error!(error = %e, "portal.payment_methods.add_failed");
            (StatusCode::INTERNAL_SERVER_ERROR, "Add failed").into_response()
        }
    }
}

// ── POST /payment-methods/{pm_id}/{remove,set-default} ───────────────────────

pub async fn remove_method(
    state: State<AppState>,
    portal: Extension<PortalSession>,
    path: Path<String>,
    headers: HeaderMap,
    form: RawForm,
) -> Response {
    method_mutation(
        state,
        portal,
        path,
        headers,
        form,
        "payment_method_remove",
        "remove",
        "removed",
    )
    .await
}

pub async fn set_default(
    state: State<AppState>,
    portal: Extension<PortalSession>,
    path: Path<String>,
    headers: HeaderMap,
    form: RawForm,
) -> Response {
    method_mutation(
        state,
        portal,
        path,
        headers,
        form,
        "payment_method_set_default",
        "set-default",
        "default_set",
    )
    .await
}

#[allow(clippy::too_many_arguments)]
async fn method_mutation(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Path(pm_id): Path<String>,
    headers: HeaderMap,
    RawForm(body): RawForm,
    action: &str,
    verb: &str,
    flash: &str,
) -> Response {
    let form = parse_form_pairs(&body);
    let customer_id = match require_linked_customer(&portal, "/payment-methods") {
        Ok(c) => c,
        Err(r) => return r,
    };
    let route = format!("/payment-methods/{pm_id}/{verb}");
    if let Err(r) = check_step_up(&state, &portal, action, &headers, &form, &route).await {
        return r;
    }
    let identity_id = portal
        .identity
        .as_ref()
        .map(|i| i.id.clone())
        .unwrap_or_default();
    let ua = ua_of(&headers);

    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };

    // Ownership check against the customer's active methods.
    let owned = clients
        .payment
        .list_methods(&customer_id)
        .await
        .ok()
        .and_then(|v| v.as_array().cloned())
        .unwrap_or_default()
        .into_iter()
        .any(|m| m.get("id").and_then(Value::as_str) == Some(pm_id.as_str()));
    if !owned {
        audit_action(
            &state,
            &customer_id,
            &identity_id,
            action,
            &route,
            false,
            Some(OWNERSHIP_RULE),
            true,
            ua.as_deref(),
        )
        .await;
        let mut resp = render(
            &state,
            "payment_methods_forbidden.html",
            context! {
                customer_facing => render_rule(OWNERSHIP_RULE),
                request => request_ctx("/payment-methods", portal.identity_email()),
            },
        );
        *resp.status_mut() = StatusCode::FORBIDDEN;
        return resp;
    }

    let result = if verb == "remove" {
        clients.payment.remove_method(&pm_id).await
    } else {
        clients.payment.set_default_method(&pm_id).await
    };
    match result {
        Ok(_) => {
            audit_action(
                &state,
                &customer_id,
                &identity_id,
                action,
                &route,
                true,
                None,
                true,
                ua.as_deref(),
            )
            .await;
            Redirect::to(&format!("/payment-methods?flash={flash}")).into_response()
        }
        Err(ClientError::Policy(pv)) => {
            audit_action(
                &state,
                &customer_id,
                &identity_id,
                action,
                &route,
                false,
                Some(&pv.rule),
                true,
                ua.as_deref(),
            )
            .await;
            if !is_known(&pv.rule) {
                tracing::info!(rule = %pv.rule, "portal.payment_methods.unknown_policy_rule");
            }
            render_list(
                &state,
                &portal,
                &customer_id,
                Some(render_rule(&pv.rule)),
                None,
                StatusCode::UNPROCESSABLE_ENTITY,
            )
            .await
        }
        Err(e) => {
            tracing::error!(error = %e, "portal.payment_methods.mutation_failed");
            (StatusCode::INTERNAL_SERVER_ERROR, "Failed").into_response()
        }
    }
}
