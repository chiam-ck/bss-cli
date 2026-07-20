//! `/payment-methods` — list + add (mock card form) + remove + set-default. Port
//! of `bss_self_serve.routes.payment_methods` (the mock-mode paths).
//!
//! Add/remove/set-default are step-up-gated. The mock add tokenizes the PAN
//! server-side (v0.10 behaviour); the Stripe Checkout add flow
//! (`checkout-init`/`checkout-return`) mints a `mode=setup` Checkout Session and
//! registers the resulting `pm_*` on return (same shape as the signup COF flow).
//! One `bss-clients` write per route; `portal_action` on success + failure.

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
use crate::signup::{
    local_tokenize, stripe_create_checkout_session, stripe_extract_pm,
    stripe_retrieve_checkout_session,
};
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

// ── Stripe Checkout add flow (prod: BSS_PAYMENT_PROVIDER=stripe) ──────────────
//
// The stripe add template posts "Continue to Stripe" straight to
// `/payment-methods/add/checkout-init`; that route step-up-gates, mints a
// `mode=setup` Checkout Session, and 303-redirects to Stripe's hosted card
// form. Stripe redirects the customer back to `/payment-methods/add/checkout-
// return?cs_id=cs_...`, where we pull the saved `pm_*` and register it. Mirrors
// `signup.rs`'s COF checkout and the retired python-legacy routes.

/// `?cs_id=cs_...` — Stripe fills `{CHECKOUT_SESSION_ID}` into the return URL.
#[derive(serde::Deserialize)]
pub struct CheckoutReturnQuery {
    cs_id: String,
}

/// `POST /payment-methods/add/checkout-init` — step-up, ensure `cus_*`, mint the
/// Checkout Session, 303 to Stripe.
pub async fn add_checkout_init(
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
        "/payment-methods/add/checkout-init",
    )
    .await
    {
        return r;
    }

    let render_err = |state: &AppState, msg: &str| -> Response {
        let mut r = render(
            state,
            add_template("stripe"),
            context! {
                error => msg,
                fields => serde_json::json!({}),
                payment_provider => "stripe",
                request => request_ctx("/payment-methods", portal.identity_email()),
            },
        );
        *r.status_mut() = StatusCode::SERVICE_UNAVAILABLE;
        r
    };

    let api_key = state.settings.payment_stripe_api_key.clone();
    if api_key.is_empty() {
        return render_err(
            &state,
            "Payment is misconfigured (Stripe key missing). Please contact support.",
        );
    }
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };

    // Seed Stripe's customer with the verified login email (fallback synthetic).
    let email = portal
        .identity_email()
        .filter(|s| !s.is_empty())
        .map(String::from)
        .unwrap_or_else(|| format!("{customer_id}@bss-cli.local"));
    let cus_id = match clients.payment.ensure_customer(&customer_id, &email).await {
        Ok(v) => v
            .get("customer_external_ref")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        Err(e) => {
            tracing::warn!(error = %e, "portal.payment_methods.ensure_customer_failed");
            return render_err(
                &state,
                "Couldn't reach the payment service. Please try again.",
            );
        }
    };

    let public_url = state.settings.public_url.trim_end_matches('/');
    let return_url =
        format!("{public_url}/payment-methods/add/checkout-return?cs_id={{CHECKOUT_SESSION_ID}}");
    let cancel_url = format!("{public_url}/payment-methods");
    // No signup session here — pass an empty marker; the return handler validates
    // on `metadata[bss_customer_id]` instead.
    match stripe_create_checkout_session(
        &api_key,
        &cus_id,
        &return_url,
        &cancel_url,
        &customer_id,
        "",
    )
    .await
    {
        Ok(url) => {
            tracing::info!(customer_id = %customer_id, "portal.payment_methods.checkout_redirect");
            Redirect::to(&url).into_response()
        }
        Err(e) => {
            tracing::warn!(error = %e, "portal.payment_methods.checkout_session_create_failed");
            render_err(&state, "Couldn't reach Stripe. Please try again.")
        }
    }
}

/// `GET /payment-methods/add/checkout-return` — retrieve the session, pull the
/// saved `pm_*`, register it as a card on file, redirect to the list.
pub async fn add_checkout_return(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Query(q): Query<CheckoutReturnQuery>,
) -> Response {
    let customer_id = match require_linked_customer(&portal, "/payment-methods") {
        Ok(c) => c,
        Err(r) => return r,
    };
    let api_key = state.settings.payment_stripe_api_key.clone();
    if api_key.is_empty() || !q.cs_id.starts_with("cs_") {
        return Redirect::to("/payment-methods?flash=error").into_response();
    }

    let cs = match stripe_retrieve_checkout_session(&api_key, &q.cs_id).await {
        Ok(v) => v,
        Err(e) => {
            tracing::warn!(error = %e, cs_id = %q.cs_id, "portal.payment_methods.checkout_session_retrieve_failed");
            return Redirect::to("/payment-methods?flash=error").into_response();
        }
    };

    // Defence in depth: refuse a session that isn't this customer's.
    let meta_customer = cs
        .get("metadata")
        .and_then(|m| m.get("bss_customer_id"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    if meta_customer != customer_id {
        tracing::warn!(cs_id = %q.cs_id, "portal.payment_methods.checkout_metadata_mismatch");
        return Redirect::to("/payment-methods?flash=error").into_response();
    }

    let pm_id = match stripe_extract_pm(&api_key, cs.get("setup_intent")).await {
        Some(pm) if pm.starts_with("pm_") => pm,
        _ => {
            tracing::warn!(cs_id = %q.cs_id, "portal.payment_methods.checkout_no_pm");
            return Redirect::to("/payment-methods?flash=error").into_response();
        }
    };

    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };
    match clients
        .payment
        .create_stripe_payment_method(&customer_id, &pm_id)
        .await
    {
        Ok(_) => Redirect::to("/payment-methods?flash=added").into_response(),
        Err(ClientError::Policy(pv)) => {
            if !is_known(&pv.rule) {
                tracing::info!(rule = %pv.rule, "portal.payment_methods.unknown_policy_rule");
            }
            Redirect::to(&format!("/payment-methods?flash=error&rule={}", pv.rule)).into_response()
        }
        Err(e) => {
            tracing::error!(error = %e, "portal.payment_methods.checkout_register_failed");
            Redirect::to("/payment-methods?flash=error").into_response()
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
