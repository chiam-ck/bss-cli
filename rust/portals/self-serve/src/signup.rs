//! Signup funnel — the deterministic direct-write chain. Port of
//! `bss_self_serve.routes.signup`.
//!
//! **This slice (P6b s5):** the entry surface — the signup form, the live promo
//! preview, `POST /signup` (step 1: `crm.create_customer` + identity link), and
//! the progress page that hosts the HTMX step timeline. The KYC/COF/order/poll
//! step routes + the Stripe-checkout and Didit-handoff variants land in the next
//! slice.
//!
//! Doctrine: `identity.email` is the only source of email (the form never
//! carries it); one `bss-clients` write per step; a `portal_action` audit row
//! per write (success and failure); structured policy violations render via the
//! shared [`error_messages`] map.

use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Redirect, Response};
use axum::{Extension, Form};
use bss_clients::ClientError;
use minijinja::context;
use serde::Deserialize;
use serde_json::Value;

use bss_portal_auth::{record_portal_action, LinkError, PortalActionRecord};

use crate::deps::require_verified_email;
use crate::error_messages::{is_known, render as render_rule};
use crate::middleware::PortalSession;
use crate::offerings::{find_plan, flatten_offerings};
use crate::prompts::KYC_PREBAKED_ATTESTATION_ID;
use crate::routes::render;
use crate::signup_session::{CreateArgs, SignupSession, SignupStep};
use crate::templating::request_ctx;
use crate::AppState;

// ── helpers ──────────────────────────────────────────────────────────────────

/// `+65 8000 1111` for the canonical 8-digit SG mobile; otherwise verbatim.
fn format_msisdn(msisdn: &str) -> String {
    if msisdn.len() == 8 && msisdn.chars().all(|c| c.is_ascii_digit()) {
        format!("+65 {} {}", &msisdn[..4], &msisdn[4..])
    } else {
        msisdn.to_string()
    }
}

fn user_agent(headers: &HeaderMap) -> Option<String> {
    headers
        .get(axum::http::header::USER_AGENT)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string)
}

fn payment_provider(state: &AppState) -> &str {
    &state.settings.payment_provider
}

/// One `portal_action` row per step (success or failure). `db` may be `None`
/// (template-only tests) — then this is a no-op, matching the Python
/// `factory is None` guard. `ip` is `None` until `ConnectInfo` is wired.
#[allow(clippy::too_many_arguments)]
async fn record_step(
    state: &AppState,
    sig: &SignupSession,
    customer_id: Option<&str>,
    action: &str,
    route: &str,
    success: bool,
    error_rule: Option<&str>,
    user_agent: Option<&str>,
) {
    let Some(pool) = &state.db else { return };
    let rec = PortalActionRecord {
        customer_id,
        identity_id: sig.identity_id.as_deref(),
        action,
        route,
        method: "POST",
        success,
        error_rule,
        step_up_consumed: false,
        ip: None,
        user_agent,
    };
    if let Err(e) = record_portal_action(pool, &rec).await {
        tracing::warn!(action = action, error = %e, "portal.signup.audit_failed");
    }
}

/// POST /signup failure path — re-render the form with a structured error (422).
async fn render_failed(state: &AppState, sig: &SignupSession, rule: &str) -> Response {
    let plan: Value = match &state.clients {
        Some(c) => match c.catalog.list_offerings().await {
            Ok(raw) => {
                let arr = raw.as_array().cloned().unwrap_or_default();
                match find_plan(&flatten_offerings(&arr), &sig.plan) {
                    Some(p) => serde_json::to_value(&p).unwrap_or(Value::Null),
                    None => context_plan_fallback(&sig.plan),
                }
            }
            Err(_) => context_plan_fallback(&sig.plan),
        },
        None => context_plan_fallback(&sig.plan),
    };
    let mut resp = render(
        state,
        "signup.html",
        context! {
            plan => plan,
            msisdn => sig.msisdn,
            msisdn_display => format_msisdn(&sig.msisdn),
            kyc_attestation_id => KYC_PREBAKED_ATTESTATION_ID,
            identity_email => sig.email,
            prefill_name => "",
            is_returning => false,
            returning_needs_card => false,
            assigned_offer => Option::<Value>::None,
            payment_provider => payment_provider(state),
            error => render_rule(rule),
            request => request_ctx("/signup", Some(&sig.email)),
        },
    );
    *resp.status_mut() = StatusCode::UNPROCESSABLE_ENTITY;
    resp
}

/// `{"id": plan, "name": plan}` — the Python `plan or {...}` fallback shape.
fn context_plan_fallback(plan_id: &str) -> Value {
    serde_json::json!({ "id": plan_id, "name": plan_id })
}

/// Extract a string `id` from a create-customer response.
fn json_str<'a>(v: &'a Value, key: &str) -> Option<&'a str> {
    v.get(key).and_then(Value::as_str)
}

// ── GET /signup/{plan_id} ────────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct SignupFormQuery {
    msisdn: String,
}

pub async fn signup_form(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Path(plan_id): Path<String>,
    Query(q): Query<SignupFormQuery>,
) -> Response {
    let next = format!("/signup/{plan_id}?msisdn={}", q.msisdn);
    let identity = match require_verified_email(&portal, &next) {
        Ok(i) => i,
        Err(r) => return r,
    };
    // Shape validation on msisdn (Python enforces via the route pattern).
    if !(6..=15).contains(&q.msisdn.len()) || !q.msisdn.chars().all(|c| c.is_ascii_digit()) {
        return (StatusCode::UNPROCESSABLE_ENTITY, "Invalid msisdn").into_response();
    }

    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Catalog unavailable").into_response();
    };

    let raw = match clients.catalog.list_offerings().await {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!(error = %e, "portal.signup.catalog_read_failed");
            return (StatusCode::BAD_GATEWAY, "Catalog unavailable").into_response();
        }
    };
    let arr = raw.as_array().cloned().unwrap_or_default();
    let plan = match find_plan(&flatten_offerings(&arr), &plan_id) {
        Some(p) => p,
        None => return (StatusCode::NOT_FOUND, format!("Unknown plan: {plan_id}")).into_response(),
    };

    // Returning-customer second-line UX — best-effort reads.
    let is_returning = identity.customer_id.is_some();
    let mut prefill_name = String::new();
    let mut returning_needs_card = false;
    let mut assigned_offer: Option<Value> = None;
    if let Some(cid) = &identity.customer_id {
        if let Ok(cust) = clients.crm.get_customer(cid).await {
            if let Some(ind) = cust.get("individual") {
                let given = ind
                    .get("givenName")
                    .or_else(|| ind.get("given_name"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let family = ind
                    .get("familyName")
                    .or_else(|| ind.get("family_name"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                prefill_name = format!("{given} {family}").trim().to_string();
            }
        }
        // A linked identity doesn't guarantee a card on file.
        if let Ok(methods) = clients.payment.list_methods(cid).await {
            returning_needs_card = methods.as_array().map(|a| a.is_empty()).unwrap_or(false);
        }
        // Best applicable assigned offer, pre-applied with a remove toggle.
        if let Ok(res) = clients.catalog.resolve_eligible_promo(cid, &plan_id).await {
            if res.get("valid").and_then(Value::as_bool).unwrap_or(false) {
                assigned_offer = Some(res);
            }
        }
    }

    render(
        &state,
        "signup.html",
        context! {
            plan => minijinja::Value::from_serialize(&plan),
            msisdn => q.msisdn,
            msisdn_display => format_msisdn(&q.msisdn),
            kyc_attestation_id => KYC_PREBAKED_ATTESTATION_ID,
            identity_email => identity.email,
            prefill_name => prefill_name,
            is_returning => is_returning,
            returning_needs_card => returning_needs_card,
            assigned_offer => assigned_offer,
            payment_provider => payment_provider(&state),
            request => request_ctx("/signup", Some(&identity.email)),
        },
    )
}

// ── GET /signup/promo/preview ────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct PromoPreviewQuery {
    offering: String,
    #[serde(default)]
    code: String,
    #[serde(default)]
    promo_code: String,
    #[serde(default)]
    has_offer: String,
}

pub async fn signup_promo_preview(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Query(q): Query<PromoPreviewQuery>,
) -> Response {
    let identity = match require_verified_email(&portal, "/plans") {
        Ok(i) => i,
        Err(r) => return r,
    };
    // The form field is `promo_code`; accept `code` too. The form's param wins.
    let code = if !q.promo_code.trim().is_empty() {
        q.promo_code.trim()
    } else {
        q.code.trim()
    };
    if code.is_empty() {
        return axum::response::Html(String::new()).into_response();
    }

    let result = match &state.clients {
        Some(c) => c
            .catalog
            .preview_promo(code, &q.offering, identity.customer_id.as_deref())
            .await
            .unwrap_or_else(|_| {
                tracing::warn!(offering = %q.offering, "signup.promo_preview.failed");
                serde_json::json!({ "valid": false, "reason": Value::Null })
            }),
        None => serde_json::json!({ "valid": false, "reason": Value::Null }),
    };

    render(
        &state,
        "partials/promo_preview.html",
        context! {
            valid => result.get("valid").and_then(Value::as_bool).unwrap_or(false),
            code => code,
            label => result.get("label").cloned().unwrap_or(Value::Null),
            base => result.get("base").cloned().unwrap_or(Value::Null),
            effective => result.get("effective").cloned().unwrap_or(Value::Null),
            reason => result.get("reason").cloned().unwrap_or(Value::Null),
            has_offer => !q.has_offer.is_empty(),
        },
    )
}

// ── POST /signup — step 1: create customer + link identity ───────────────────

#[derive(Deserialize)]
pub struct SignupSubmitForm {
    plan: String,
    name: String,
    phone: String,
    msisdn: String,
    #[serde(default)]
    card_pan: String,
    #[serde(default)]
    promo_code: String,
    #[serde(default)]
    offer_shown: String,
    #[serde(default)]
    apply_offer: String,
}

pub async fn signup_submit(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    headers: HeaderMap,
    Form(form): Form<SignupSubmitForm>,
) -> Response {
    let identity = match require_verified_email(&portal, "/plans") {
        Ok(i) => i,
        Err(r) => return r,
    };
    let ua = user_agent(&headers);
    let provider = state.settings.payment_provider.clone();

    let mut sig = state.signup_store.create(CreateArgs {
        plan: form.plan.clone(),
        name: form.name.clone(),
        email: identity.email.clone(),
        phone: form.phone.clone(),
        msisdn: form.msisdn.clone(),
        card_pan: form.card_pan.clone(),
        identity_id: Some(identity.id.clone()),
        promo_code: form.promo_code.clone(),
        skip_assigned_offer: !form.offer_shown.is_empty() && form.apply_offer.is_empty(),
    });

    let existing_customer_id = identity.customer_id.clone();

    // New customers MUST supply a PAN in mock mode (the COF step needs it).
    // Stripe mode collects the card at the COF step (PCI doctrine).
    if existing_customer_id.is_none() && provider == "mock" && form.card_pan.trim().is_empty() {
        state.signup_store.update(&sig);
        return render_failed(&state, &sig, "policy.payment.method.invalid_card").await;
    }

    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Signup unavailable").into_response();
    };

    // ── returning customer: reuse the linked CUST, resume at first incomplete step ──
    if let Some(cid) = existing_customer_id {
        sig.customer_id = Some(cid.clone());

        let kyc = clients.crm.get_kyc_status(&cid).await;
        let methods = clients.payment.list_methods(&cid).await;

        // Either a Policy violation → fail the step (Python catches only that).
        for res in [&kyc, &methods] {
            if let Err(ClientError::Policy(pv)) = res {
                record_step(
                    &state,
                    &sig,
                    Some(&cid),
                    "signup_create_customer",
                    "/signup",
                    false,
                    Some(&pv.rule),
                    ua.as_deref(),
                )
                .await;
                if !is_known(&pv.rule) {
                    tracing::info!(rule = %pv.rule, "portal.signup.unknown_policy_rule");
                }
                sig.step = SignupStep::Failed;
                sig.step_error = Some(pv.rule.clone());
                state.signup_store.update(&sig);
                return render_failed(&state, &sig, &pv.rule).await;
            }
        }
        // Non-policy transport/server errors bubble to a 500 (Python lets them propagate).
        let (kyc, methods) = match (kyc, methods) {
            (Ok(k), Ok(m)) => (k, m),
            _ => {
                return (StatusCode::INTERNAL_SERVER_ERROR, "Signup lookup failed").into_response()
            }
        };

        let kyc_verified = kyc.get("kyc_status").and_then(Value::as_str) == Some("verified");
        let no_methods = methods.as_array().map(|a| a.is_empty()).unwrap_or(true);

        if provider == "mock" && no_methods && form.card_pan.trim().is_empty() {
            state.signup_store.update(&sig);
            return render_failed(&state, &sig, "policy.payment.method.invalid_card").await;
        }

        sig.step = if !kyc_verified {
            SignupStep::PendingKyc
        } else if no_methods {
            SignupStep::PendingCof
        } else {
            SignupStep::PendingOrder
        };
        record_step(
            &state,
            &sig,
            Some(&cid),
            "signup_create_customer",
            "/signup",
            true,
            Some("signup.create_customer.reused_linked_identity"),
            ua.as_deref(),
        )
        .await;
        state.signup_store.update(&sig);
        return progress_redirect(&form.plan, &sig.session_id);
    }

    // ── new customer: create + atomically link the identity ──
    let customer = match clients
        .crm
        .create_customer(&form.name, Some(&identity.email), Some(&form.phone))
        .await
    {
        Ok(c) => c,
        Err(ClientError::Policy(pv)) => {
            record_step(
                &state,
                &sig,
                None,
                "signup_create_customer",
                "/signup",
                false,
                Some(&pv.rule),
                ua.as_deref(),
            )
            .await;
            if !is_known(&pv.rule) {
                tracing::info!(rule = %pv.rule, "portal.signup.unknown_policy_rule");
            }
            sig.step = SignupStep::Failed;
            sig.step_error = Some(pv.rule.clone());
            state.signup_store.update(&sig);
            return render_failed(&state, &sig, &pv.rule).await;
        }
        Err(e) => {
            tracing::error!(error = %e, "portal.signup.create_customer_failed");
            return (StatusCode::INTERNAL_SERVER_ERROR, "Signup failed").into_response();
        }
    };

    let Some(customer_id) = json_str(&customer, "id").map(str::to_string) else {
        record_step(
            &state,
            &sig,
            None,
            "signup_create_customer",
            "/signup",
            false,
            Some("signup.create_customer.no_id"),
            ua.as_deref(),
        )
        .await;
        sig.step = SignupStep::Failed;
        sig.step_error = Some("signup.create_customer.no_id".to_string());
        state.signup_store.update(&sig);
        return render_failed(&state, &sig, "signup.create_customer.no_id").await;
    };
    sig.customer_id = Some(customer_id.clone());

    // Atomically bind the verified identity to the new customer (before the rest
    // of the chain runs, so a mid-flow abandon still leaves the pair intact).
    if let Some(pool) = &state.db {
        match bss_portal_auth::link_to_customer(pool, &identity.id, &customer_id).await {
            Ok(()) => {}
            Err(LinkError::AlreadyLinked { existing }) => {
                tracing::warn!(
                    identity_id = %identity.id, customer_id = %customer_id,
                    existing = %existing, "portal.signup.link_failed",
                );
            }
            Err(e) => {
                tracing::warn!(
                    identity_id = %identity.id, customer_id = %customer_id,
                    error = %e, "portal.signup.link_failed",
                );
            }
        }
    }

    record_step(
        &state,
        &sig,
        Some(&customer_id),
        "signup_create_customer",
        "/signup",
        true,
        None,
        ua.as_deref(),
    )
    .await;
    sig.step = SignupStep::PendingKyc;
    state.signup_store.update(&sig);

    progress_redirect(&form.plan, &sig.session_id)
}

/// 303 to the progress page for `plan`/`session`.
fn progress_redirect(plan: &str, session_id: &str) -> Response {
    Redirect::to(&format!("/signup/{plan}/progress?session={session_id}")).into_response()
}

// ── GET /signup/{plan_id}/progress — the 5-step timeline host ────────────────

#[derive(Deserialize)]
pub struct ProgressQuery {
    session: String,
}

pub async fn signup_progress(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Path(plan_id): Path<String>,
    Query(q): Query<ProgressQuery>,
) -> Response {
    let identity = match require_verified_email(&portal, &format!("/signup/{plan_id}/progress")) {
        Ok(i) => i,
        Err(r) => return r,
    };
    let Some(sig) = state.signup_store.get(&q.session) else {
        return (StatusCode::NOT_FOUND, "Unknown or expired session.").into_response();
    };
    // Defence-in-depth: a logged-in user can't peek at someone else's signup.
    if let Some(sid) = &sig.identity_id {
        if *sid != identity.id {
            return (StatusCode::NOT_FOUND, "Unknown or expired session.").into_response();
        }
    }

    render(
        &state,
        "progress.html",
        context! {
            session_id => q.session,
            plan_id => plan_id,
            signup => minijinja::Value::from_serialize(&sig),
            payment_provider => payment_provider(&state),
            // Python reads app.state.payment_stripe_publishable_key, which main.py
            // never sets → always "". Checkout-redirect mode needs no client key.
            stripe_publishable_key => "",
            request => request_ctx("/signup", Some(&identity.email)),
        },
    )
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn format_msisdn_sg_mobile() {
        assert_eq!(format_msisdn("80001111"), "+65 8000 1111");
        assert_eq!(format_msisdn("123"), "123");
        assert_eq!(format_msisdn("abcdefgh"), "abcdefgh");
    }
}
