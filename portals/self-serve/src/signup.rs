//! Signup funnel — the deterministic direct-write chain. Port of
//! `bss_self_serve.routes.signup`.
//!
//! **Ported (P6b s5–s7):** the full sandbox happy path — the form, live promo
//! preview, `POST /signup` (create customer + identity link), the progress page,
//! and the five-step timeline: KYC (`step/kyc`, prebaked), card-on-file
//! (`step/cof`, mock tokenizer), order (`step/order`), and poll (`step/poll` →
//! `HX-Redirect` to `/confirmation`). **Deferred:** the Stripe-checkout COF
//! variant and the Didit hosted-UI KYC handoff (both prod-only).
//!
//! Doctrine: `identity.email` is the only source of email (the form never
//! carries it); one `bss-clients` write per step; a `portal_action` audit row
//! per write (success and failure); structured policy violations render via the
//! shared [`error_messages`] map.

use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Redirect, Response};
use axum::{Extension, Form};
use bss_clients::{AttestKycOpts, ClientError};
use minijinja::context;
use serde::Deserialize;
use serde_json::Value;

use bss_portal_auth::{record_portal_action, LinkError, PortalActionRecord};

use crate::deps::require_verified_email;
use crate::error_messages::{is_known, render as render_rule};
use crate::kyc::KycError;
use crate::middleware::PortalSession;
use crate::offerings::{find_plan, flatten_offerings};
use crate::prompts::{prebaked_signature, KYC_PREBAKED_ATTESTATION_ID};
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

// ── step-chain shared helpers ────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct SessionQuery {
    session: String,
}

/// Fetch the signup session for a step route, enforcing the owning-identity
/// check. Port of `_resolve`: 404 when unknown/expired or owned by another
/// identity.
// Err is the 404 `Response` the caller returns straight through — boxing it would
// only add churn (same rationale as the `deps` gate helpers).
#[allow(clippy::result_large_err)]
fn resolve(
    state: &AppState,
    session_id: &str,
    identity: &bss_portal_auth::IdentityView,
) -> Result<SignupSession, Response> {
    let Some(sig) = state.signup_store.get(session_id) else {
        return Err((StatusCode::NOT_FOUND, "Unknown or expired session.").into_response());
    };
    if let Some(sid) = &sig.identity_id {
        if *sid != identity.id {
            return Err((StatusCode::NOT_FOUND, "Unknown or expired session.").into_response());
        }
    }
    Ok(sig)
}

/// Render the `partials/signup_progress.html` HTMX fragment for the current
/// step. Port of `_render_step_fragment`.
fn render_step_fragment(state: &AppState, sig: &SignupSession) -> Response {
    let step_error_message = sig.step_error.as_deref().map(render_rule);
    render(
        state,
        "partials/signup_progress.html",
        context! {
            signup => minijinja::Value::from_serialize(sig),
            session_id => sig.session_id,
            plan_id => sig.plan,
            step_error_message => step_error_message,
            payment_provider => payment_provider(state),
            stripe_publishable_key => "",
        },
    )
}

// ── POST /signup/step/kyc — step 2 (prebaked sync OR Didit handoff) ──────────

pub async fn signup_step_kyc(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    headers: HeaderMap,
    Query(q): Query<SessionQuery>,
) -> Response {
    let identity = match require_verified_email(&portal, "/plans") {
        Ok(i) => i,
        Err(r) => return r,
    };
    let ua = user_agent(&headers);
    let mut sig = match resolve(&state, &q.session, &identity) {
        Ok(s) => s,
        Err(r) => return r,
    };
    if sig.step != SignupStep::PendingKyc {
        return render_step_fragment(&state, &sig);
    }

    let public_url = state.settings.public_url.trim_end_matches('/');
    let return_url = format!(
        "{public_url}/signup/step/kyc/callback?session={}",
        q.session
    );

    // Uniform async dispatch — prebaked returns immediately, Didit POSTs a
    // hosted session. `KycCapExhausted` is a hard block (no fallback, Motto).
    let kyc_session = match state.kyc_adapter.initiate(&sig.email, &return_url).await {
        Ok(s) => s,
        Err(KycError::CapExhausted(_)) => {
            tracing::warn!("portal.signup.kyc.cap_exhausted");
            sig.step = SignupStep::Failed;
            sig.step_error = Some("kyc.cap_exhausted".to_string());
            state.signup_store.update(&sig);
            return render_step_fragment(&state, &sig);
        }
        Err(e) => {
            tracing::error!(error = %e, "portal.signup.kyc.initiate_failed");
            sig.step = SignupStep::Failed;
            sig.step_error = Some("kyc.initiate_failed".to_string());
            state.signup_store.update(&sig);
            return render_step_fragment(&state, &sig);
        }
    };

    if state.kyc_adapter.is_prebaked() {
        // Synchronous: complete the attest in this request and advance.
        return complete_kyc_attest(&state, &mut sig, &kyc_session.session_id, ua.as_deref(), false)
            .await;
    }

    // Real provider (Didit) — render the cross-device handoff (URL + QR) inside
    // the progress card and let the desktop poll advance the signup when the
    // corroboration webhook arrives (NOT the post-verification redirect).
    sig.kyc_provider_session_id = Some(kyc_session.session_id.clone());
    sig.kyc_verify_url = Some(kyc_session.redirect_url.clone());
    sig.kyc_verify_qr = Some(crate::qrpng::qr_data_uri(&kyc_session.redirect_url, 8, 2));
    sig.step = SignupStep::PendingKycHandoff;
    state.signup_store.update(&sig);
    render_step_fragment(&state, &sig)
}

// ── GET /signup/step/kyc/poll — desktop poll for the corroboration row ───────

pub async fn signup_step_kyc_poll(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    headers: HeaderMap,
    Query(q): Query<SessionQuery>,
) -> Response {
    let identity = match require_verified_email(&portal, "/plans") {
        Ok(i) => i,
        Err(r) => return r,
    };
    let ua = user_agent(&headers);
    let mut sig = match resolve(&state, &q.session, &identity) {
        Ok(s) => s,
        Err(r) => return r,
    };
    // Already advanced/failed, or initiate hasn't populated the session id yet —
    // re-render the current fragment so its trigger re-arms.
    if sig.step != SignupStep::PendingKycHandoff {
        return render_step_fragment(&state, &sig);
    }
    let Some(psid) = sig
        .kyc_provider_session_id
        .clone()
        .filter(|s| !s.is_empty())
    else {
        return render_step_fragment(&state, &sig);
    };
    let Some(pool) = &state.db else {
        return render_step_fragment(&state, &sig);
    };

    // Didit progresses Not Started → In Progress/In Review → terminal (Approved /
    // Declined / Expired), updated in place by the webhook. Only act on terminal.
    let status: Option<String> = sqlx::query_scalar(
        "SELECT decision_status FROM integrations.kyc_webhook_corroboration \
         WHERE provider = 'didit' AND provider_session_id = $1",
    )
    .bind(&psid)
    .fetch_optional(pool)
    .await
    .ok()
    .flatten();

    match status.as_deref() {
        None | Some("Not Started") | Some("In Progress") | Some("In Review") => {
            render_step_fragment(&state, &sig)
        }
        Some(s @ ("Declined" | "Expired")) => {
            sig.step = SignupStep::Failed;
            sig.step_error = Some(format!("kyc.{}", s.to_lowercase()));
            state.signup_store.update(&sig);
            render_step_fragment(&state, &sig)
        }
        Some("Approved") => {
            // Complete the BSS attest — advances to pending_cof and renders the
            // next fragment (whose own trigger fires the next step).
            complete_kyc_attest(&state, &mut sig, &psid, ua.as_deref(), false).await
        }
        // Unknown terminal status — keep waiting (forward-compat).
        Some(_) => render_step_fragment(&state, &sig),
    }
}

// ── GET /signup/step/kyc/callback — return path from the hosted UI ───────────

pub async fn signup_step_kyc_callback(
    State(state): State<AppState>,
    Query(q): Query<SessionQuery>,
) -> Response {
    // Public route (allowlist) — the verifying device (often a phone WITHOUT the
    // desktop's portal session cookie) lands here. It must not require auth; it
    // just shows a friendly "verification complete, return to your computer"
    // page. The desktop's poll loop is what actually advances the signup.
    render(
        &state,
        "signup_kyc_confirmation.html",
        context! {
            session_id => q.session,
            request => request_ctx("/signup/step/kyc/callback", None),
        },
    )
}

/// Shared finisher: fetch the attestation, submit it to BSS, advance the signup.
/// Port of `_complete_kyc_attest` — used by the prebaked synchronous path and the
/// Didit poll (`redirect_after = false`). `redirect_after` 303s to the progress
/// page instead of returning a fragment (the callback-device variant).
async fn complete_kyc_attest(
    state: &AppState,
    sig: &mut SignupSession,
    kyc_session_id: &str,
    ua: Option<&str>,
    redirect_after: bool,
) -> Response {
    let attestation = match state.kyc_adapter.fetch_attestation(kyc_session_id).await {
        Ok(a) => a,
        Err(KycError::CorroborationTimeout(_)) => {
            tracing::info!(
                provider_session = kyc_session_id,
                "portal.signup.kyc.corroboration_timeout"
            );
            sig.step_error = Some("kyc.corroboration_timeout".to_string());
            state.signup_store.update(sig);
            return kyc_finish(state, sig, redirect_after);
        }
        Err(e) => {
            tracing::error!(error = %e, "portal.signup.kyc.fetch_failed");
            sig.step_error = Some("kyc.fetch_failed".to_string());
            state.signup_store.update(sig);
            return kyc_finish(state, sig, redirect_after);
        }
    };

    let customer_id = sig.customer_id.clone().unwrap_or_default();
    // Same attestation_token for both providers — the CRM distinguishes them by
    // `provider` + `corroboration_id` (Didit's trust anchor is the webhook row).
    let token = prebaked_signature(&sig.email);
    let opts = AttestKycOpts {
        provider_reference: Some(&attestation.provider_reference),
        document_type: Some(&attestation.document_type),
        document_number_last4: Some(&attestation.document_number_last4),
        document_number_hash: Some(&attestation.document_number_hash),
        document_country: Some(&attestation.document_country),
        date_of_birth: Some(&attestation.date_of_birth),
        corroboration_id: attestation.corroboration_id.as_deref(),
        ..Default::default()
    };

    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Signup unavailable").into_response();
    };

    match clients
        .crm
        .attest_kyc_full(&customer_id, &attestation.provider, &token, opts)
        .await
    {
        Ok(_) => {
            record_step(
                state,
                sig,
                Some(&customer_id),
                "signup_attest_kyc",
                "/signup/step/kyc",
                true,
                None,
                ua,
            )
            .await;
            sig.step = SignupStep::PendingCof;
            state.signup_store.update(sig);
            kyc_finish(state, sig, redirect_after)
        }
        Err(ClientError::Policy(pv)) => {
            record_step(
                state,
                sig,
                Some(&customer_id),
                "signup_attest_kyc",
                "/signup/step/kyc",
                false,
                Some(&pv.rule),
                ua,
            )
            .await;
            if !is_known(&pv.rule) {
                tracing::info!(rule = %pv.rule, "portal.signup.unknown_policy_rule");
            }
            sig.step = SignupStep::Failed;
            sig.step_error = Some(pv.rule.clone());
            state.signup_store.update(sig);
            kyc_finish(state, sig, redirect_after)
        }
        Err(e) => {
            tracing::error!(error = %e, "portal.signup.attest_kyc_failed");
            (StatusCode::INTERNAL_SERVER_ERROR, "Signup failed").into_response()
        }
    }
}

/// Return a progress redirect (callback device) or the step fragment (poll/sync).
fn kyc_finish(state: &AppState, sig: &SignupSession, redirect_after: bool) -> Response {
    if redirect_after {
        Redirect::to(&format!(
            "/signup/{}/progress?session={}",
            sig.plan, sig.session_id
        ))
        .into_response()
    } else {
        render_step_fragment(state, sig)
    }
}

// ── POST /signup/step/cof — step 3 (mock tokenizer path) ─────────────────────

pub async fn signup_step_cof(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    headers: HeaderMap,
    Query(q): Query<SessionQuery>,
) -> Response {
    let identity = match require_verified_email(&portal, "/plans") {
        Ok(i) => i,
        Err(r) => return r,
    };
    let ua = user_agent(&headers);
    let mut sig = match resolve(&state, &q.session, &identity) {
        Ok(s) => s,
        Err(r) => return r,
    };

    // Stripe Checkout (init/return) is deferred; in sandbox the provider is mock.
    if state.settings.payment_provider == "stripe" {
        tracing::warn!("portal.signup.cof.stripe_deferred");
        return render_step_fragment(&state, &sig);
    }

    if sig.step != SignupStep::PendingCof {
        return render_step_fragment(&state, &sig);
    }
    signup_step_cof_mock(&state, &mut sig, ua.as_deref()).await
}

async fn signup_step_cof_mock(
    state: &AppState,
    sig: &mut SignupSession,
    ua: Option<&str>,
) -> Response {
    let customer_id = sig.customer_id.clone().unwrap_or_default();
    let tok = match local_tokenize(&sig.card_pan) {
        Ok(t) => t,
        Err(()) => {
            record_step(
                state,
                sig,
                Some(&customer_id),
                "signup_add_card",
                "/signup/step/cof",
                false,
                Some("policy.payment.method.invalid_card"),
                ua,
            )
            .await;
            sig.step = SignupStep::Failed;
            sig.step_error = Some("policy.payment.method.invalid_card".to_string());
            state.signup_store.update(sig);
            return render_step_fragment(state, sig);
        }
    };

    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Signup unavailable").into_response();
    };

    match clients
        .payment
        .create_payment_method(
            &customer_id,
            &tok.card_token,
            &tok.last4,
            &tok.brand,
            12,
            2030,
        )
        .await
    {
        Ok(method) => {
            sig.payment_method_id = method.get("id").and_then(Value::as_str).map(str::to_string);
            // Card PAN cleared from memory the moment tokenize + add_card succeed.
            sig.card_pan = String::new();
            record_step(
                state,
                sig,
                Some(&customer_id),
                "signup_add_card",
                "/signup/step/cof",
                true,
                None,
                ua,
            )
            .await;
            sig.step = SignupStep::PendingOrder;
            state.signup_store.update(sig);
            render_step_fragment(state, sig)
        }
        Err(ClientError::Policy(pv)) => {
            record_step(
                state,
                sig,
                Some(&customer_id),
                "signup_add_card",
                "/signup/step/cof",
                false,
                Some(&pv.rule),
                ua,
            )
            .await;
            if !is_known(&pv.rule) {
                tracing::info!(rule = %pv.rule, "portal.signup.unknown_policy_rule");
            }
            sig.step = SignupStep::Failed;
            sig.step_error = Some(pv.rule.clone());
            state.signup_store.update(sig);
            render_step_fragment(state, sig)
        }
        Err(e) => {
            tracing::error!(error = %e, "portal.signup.add_card_failed");
            (StatusCode::INTERNAL_SERVER_ERROR, "Signup failed").into_response()
        }
    }
}

// ── POST /signup/step/order — step 4 ─────────────────────────────────────────

pub async fn signup_step_order(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    headers: HeaderMap,
    Query(q): Query<SessionQuery>,
) -> Response {
    let identity = match require_verified_email(&portal, "/plans") {
        Ok(i) => i,
        Err(r) => return r,
    };
    let ua = user_agent(&headers);
    let mut sig = match resolve(&state, &q.session, &identity) {
        Ok(s) => s,
        Err(r) => return r,
    };
    if sig.step != SignupStep::PendingOrder {
        return render_step_fragment(&state, &sig);
    }
    let customer_id = sig.customer_id.clone().unwrap_or_default();
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Signup unavailable").into_response();
    };

    // create + submit are one conceptual write (mirrors order.create). A failure
    // in either — or a missing order id — flips the step to failed.
    let discount = Some(sig.promo_code.as_str()).filter(|s| !s.is_empty());
    let created = clients
        .com
        .create_order(
            &customer_id,
            &sig.plan,
            Some(sig.msisdn.as_str()),
            None,
            discount,
            sig.skip_assigned_offer,
        )
        .await;

    let order_id = match &created {
        Ok(c) => c.get("id").and_then(Value::as_str).map(str::to_string),
        Err(ClientError::Policy(_)) => None,
        Err(e) => {
            tracing::error!(error = %e, "portal.signup.create_order_failed");
            return (StatusCode::INTERNAL_SERVER_ERROR, "Signup failed").into_response();
        }
    };

    // Resolve the failure rule: a policy violation from create, or a missing id.
    let submit_result = match (&created, &order_id) {
        (Ok(_), Some(oid)) => Some(clients.com.submit_order(oid).await),
        _ => None,
    };

    let fail_rule: Option<String> = match (&created, &order_id, &submit_result) {
        (Err(ClientError::Policy(pv)), _, _) => Some(pv.rule.clone()),
        (Ok(_), None, _) => Some("signup.create_order.no_id".to_string()),
        (Ok(_), Some(_), Some(Err(ClientError::Policy(pv)))) => Some(pv.rule.clone()),
        (Ok(_), Some(_), Some(Err(e))) => {
            tracing::error!(error = %e, "portal.signup.submit_order_failed");
            return (StatusCode::INTERNAL_SERVER_ERROR, "Signup failed").into_response();
        }
        _ => None,
    };

    if let Some(rule) = fail_rule {
        record_step(
            &state,
            &sig,
            Some(&customer_id),
            "signup_create_order",
            "/signup/step/order",
            false,
            Some(&rule),
            ua.as_deref(),
        )
        .await;
        if !is_known(&rule) {
            tracing::info!(rule = %rule, "portal.signup.unknown_policy_rule");
        }
        sig.step = SignupStep::Failed;
        sig.step_error = Some(rule);
        state.signup_store.update(&sig);
        return render_step_fragment(&state, &sig);
    }

    sig.order_id = order_id;
    record_step(
        &state,
        &sig,
        Some(&customer_id),
        "signup_create_order",
        "/signup/step/order",
        true,
        None,
        ua.as_deref(),
    )
    .await;
    sig.step = SignupStep::PendingActivation;
    state.signup_store.update(&sig);
    render_step_fragment(&state, &sig)
}

// ── GET /signup/step/poll — step 5 (read-only) ───────────────────────────────

pub async fn signup_step_poll(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Query(q): Query<SessionQuery>,
) -> Response {
    let identity = match require_verified_email(&portal, "/plans") {
        Ok(i) => i,
        Err(r) => return r,
    };
    let mut sig = match resolve(&state, &q.session, &identity) {
        Ok(s) => s,
        Err(r) => return r,
    };

    // Second detection (celebration dwell elapsed) — emit the HX-Redirect now.
    if sig.step == SignupStep::Completed && sig.redirect_armed && sig.subscription_id.is_some() {
        let sub = sig.subscription_id.as_deref().unwrap_or_default();
        return hx_redirect(&format!("/confirmation/{sub}?session={}", sig.session_id));
    }
    if matches!(sig.step, SignupStep::Completed | SignupStep::Failed) {
        return render_step_fragment(&state, &sig);
    }
    if sig.step != SignupStep::PendingActivation || sig.order_id.is_none() {
        return render_step_fragment(&state, &sig);
    }

    let order_id = sig.order_id.clone().unwrap_or_default();
    let Some(clients) = &state.clients else {
        return render_step_fragment(&state, &sig);
    };
    // Best-effort poll — a transient error just retriggers on the next tick.
    let order = match clients.com.get_order(&order_id).await {
        Ok(o) => o,
        Err(_) => return render_step_fragment(&state, &sig),
    };

    let stt = order.get("state").and_then(Value::as_str);
    match stt {
        Some("completed") => {
            let Some(sub_id) = extract_subscription_id(&order) else {
                // Order completed on COM but targetSubscriptionId not stamped yet
                // (SOM/COM event race) — treat as in-progress, retrigger.
                return render_step_fragment(&state, &sig);
            };
            sig.subscription_id = Some(sub_id);
            sig.activation_code = extract_activation_code(&order);
            sig.step = SignupStep::Completed;
            sig.done = true;
            sig.redirect_armed = true;
            state.signup_store.update(&sig);
            // First detection — render the celebration fragment; its 1.5s delayed
            // re-trigger lands on the redirect_armed branch above next tick.
            render_step_fragment(&state, &sig)
        }
        Some(s @ ("failed" | "cancelled")) => {
            sig.step = SignupStep::Failed;
            sig.step_error = Some(format!("order.{s}"));
            state.signup_store.update(&sig);
            render_step_fragment(&state, &sig)
        }
        _ => render_step_fragment(&state, &sig),
    }
}

/// Empty body carrying an `HX-Redirect` header (HTMX swaps the whole page).
fn hx_redirect(url: &str) -> Response {
    let mut resp = axum::response::Html(String::new()).into_response();
    if let Ok(v) = axum::http::HeaderValue::from_str(url) {
        resp.headers_mut()
            .insert(axum::http::HeaderName::from_static("hx-redirect"), v);
    }
    resp
}

// ── helpers: tokenizer + order-payload extraction ────────────────────────────

/// The mock client-side tokenizer output.
pub(crate) struct TokenizedCard {
    pub card_token: String,
    pub last4: String,
    pub brand: String,
}

/// Sandbox client-side tokenizer. Mirrors `_local_tokenize`: derives brand + last4
/// and embeds `FAIL`/`DECLINE` in the token so the payment mock can simulate
/// declines. `Err(())` for a non-numeric or too-short PAN.
pub(crate) fn local_tokenize(card_number: &str) -> Result<TokenizedCard, ()> {
    let digits: String = card_number
        .chars()
        .filter(|c| *c != ' ' && *c != '-')
        .collect();
    if !digits.chars().all(|c| c.is_ascii_digit()) || digits.len() < 12 {
        return Err(());
    }
    let last4: String = digits[digits.len() - 4..].to_string();
    let bin2 = &digits[..2];
    let brand = if digits.starts_with('4') {
        "visa"
    } else if bin2
        .parse::<u32>()
        .map(|n| (51..=55).contains(&n))
        .unwrap_or(false)
    {
        "mastercard"
    } else if bin2 == "34" || bin2 == "37" {
        "amex"
    } else {
        "unknown"
    };
    let uid = uuid::Uuid::new_v4().to_string();
    let upper = card_number.to_uppercase();
    let card_token = if upper.contains("FAIL") {
        format!("tok_FAIL_{uid}")
    } else if upper.contains("DECLINE") {
        format!("tok_DECLINE_{uid}")
    } else {
        format!("tok_{uid}")
    };
    Ok(TokenizedCard {
        card_token,
        last4,
        brand: brand.to_string(),
    })
}

const SUB_ID_KEYS: &[&str] = &[
    "targetSubscriptionId",
    "target_subscription_id",
    "subscriptionId",
    "subscription_id",
];

/// Pull the `SUB-*` id off a completed order payload (top-level keys, then each
/// item). Port of `_extract_subscription_id`.
fn extract_subscription_id(order: &Value) -> Option<String> {
    for key in SUB_ID_KEYS {
        if let Some(v) = order.get(*key).and_then(Value::as_str) {
            if v.starts_with("SUB-") {
                return Some(v.to_string());
            }
        }
    }
    if let Some(items) = order.get("items").and_then(Value::as_array) {
        for item in items {
            for key in SUB_ID_KEYS {
                if let Some(v) = item.get(*key).and_then(Value::as_str) {
                    if v.starts_with("SUB-") {
                        return Some(v.to_string());
                    }
                }
            }
        }
    }
    None
}

const ACTIVATION_KEYS: &[&str] = &["activationCode", "activation_code", "lpa"];

/// Best-effort lift of the LPA activation code off the order. Port of
/// `_extract_activation_code`.
fn extract_activation_code(order: &Value) -> Option<String> {
    for key in ACTIVATION_KEYS {
        if let Some(v) = order.get(*key).and_then(Value::as_str) {
            return Some(v.to_string());
        }
    }
    if let Some(items) = order.get("items").and_then(Value::as_array) {
        for item in items {
            for key in ACTIVATION_KEYS {
                if let Some(v) = item.get(*key).and_then(Value::as_str) {
                    return Some(v.to_string());
                }
            }
        }
    }
    None
}

// ── GET /api/session/{session_id} — JSON projection (scenario runner) ─────────

/// Read-only JSON of the in-memory signup session. Public (no session): the
/// scenario runner's HTTP step polls it for `done` + the resulting ids. Port of
/// `bss_self_serve.routes.session_api`.
pub async fn session_status(
    State(state): State<AppState>,
    Path(session_id): Path<String>,
) -> Response {
    match state.signup_store.get(&session_id) {
        Some(sig) => axum::Json(serde_json::json!({
            "session_id": sig.session_id,
            "plan": sig.plan,
            "msisdn_preference": sig.msisdn,
            "done": sig.done,
            "error": sig.error,
            "customer_id": sig.customer_id,
            "order_id": sig.order_id,
            "subscription_id": sig.subscription_id,
            "activation_code": sig.activation_code,
        }))
        .into_response(),
        None => (StatusCode::NOT_FOUND, "Unknown or expired session.").into_response(),
    }
}

// ── GET /signup/{plan_id}/msisdn — the number picker (pre-signup) ─────────────

#[derive(Deserialize)]
pub struct MsisdnPickerQuery {
    prefix: Option<String>,
    #[serde(default = "default_limit")]
    limit: i64,
}

fn default_limit() -> i64 {
    12
}

pub async fn msisdn_picker(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Path(plan_id): Path<String>,
    Query(q): Query<MsisdnPickerQuery>,
) -> Response {
    let identity = match require_verified_email(&portal, &format!("/signup/{plan_id}/msisdn")) {
        Ok(i) => i,
        Err(r) => return r,
    };
    let limit = q.limit.clamp(1, 40);
    let prefix = q
        .prefix
        .as_deref()
        .filter(|p| p.chars().all(|c| c.is_ascii_digit()));

    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };
    let raw = match clients.catalog.list_offerings().await {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!(error = %e, "portal.msisdn.catalog_failed");
            return (StatusCode::BAD_GATEWAY, "Catalog unavailable").into_response();
        }
    };
    let arr = raw.as_array().cloned().unwrap_or_default();
    let plan = match find_plan(&flatten_offerings(&arr), &plan_id) {
        Some(p) => p,
        None => return (StatusCode::NOT_FOUND, format!("Unknown plan: {plan_id}")).into_response(),
    };

    let numbers_raw = clients
        .inventory
        .list_msisdns(Some("available"), prefix, limit)
        .await
        .ok()
        .and_then(|v| v.as_array().cloned())
        .unwrap_or_default();
    let numbers: Vec<Value> = numbers_raw
        .iter()
        .filter_map(|n| n.get("msisdn").and_then(Value::as_str))
        .map(|m| {
            let display = if m.len() == 8 && m.chars().all(|c| c.is_ascii_digit()) {
                format!("{} {}", &m[..4], &m[4..])
            } else {
                m.to_string()
            };
            serde_json::json!({ "raw": m, "display": display })
        })
        .collect();

    render(
        &state,
        "msisdn_picker.html",
        context! {
            plan => minijinja::Value::from_serialize(&plan),
            numbers => minijinja::Value::from_serialize(&numbers),
            prefix => q.prefix.clone().unwrap_or_default(),
            request => request_ctx("/signup", Some(&identity.email)),
        },
    )
}

// ── GET /confirmation/{subscription_id} — post-signup eSIM QR + summary ───────

pub async fn confirmation(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Path(subscription_id): Path<String>,
    Query(q): Query<ProgressQuery>,
) -> Response {
    // Capability = the in-memory signup session id (Python has no auth dep here;
    // the browser still carries the session cookie post-signup).
    if let Err(r) = crate::deps::require_session(&portal, "/") {
        return r;
    }
    let Some(sig) = state.signup_store.get(&q.session) else {
        return (StatusCode::NOT_FOUND, "Unknown or expired session.").into_response();
    };
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };

    let subscription = clients
        .subscription
        .get(&subscription_id)
        .await
        .unwrap_or(Value::Null);

    // Activation code: prefer the one captured on the signup session; else derive
    // from the subscription's ICCID via inventory.
    let mut activation_code = sig.activation_code.clone();
    if activation_code.is_none() {
        if let Some(iccid) = subscription.get("iccid").and_then(Value::as_str) {
            if let Ok(payload) = clients.inventory.get_activation_code(iccid).await {
                activation_code = payload
                    .get("activation_code")
                    .or_else(|| payload.get("activationCode"))
                    .and_then(Value::as_str)
                    .map(str::to_string);
            }
        }
    }
    let qr = activation_code
        .as_deref()
        .map(crate::qrpng::activation_qr_data_uri)
        .unwrap_or_default();

    let plan = clients
        .catalog
        .list_offerings()
        .await
        .ok()
        .and_then(|v| v.as_array().cloned())
        .and_then(|arr| find_plan(&flatten_offerings(&arr), &sig.plan));

    render(
        &state,
        "confirmation.html",
        context! {
            subscription_id => subscription_id,
            subscription => subscription,
            activation_code => activation_code,
            qr_data_uri => qr,
            plan => plan.map(|p| minijinja::Value::from_serialize(&p)),
            signup => minijinja::Value::from_serialize(&sig),
            session_id => sig.session_id,
            plan_id => sig.plan,
            step_error_message => Option::<String>::None,
            progress_with_trigger => false,
            request => request_ctx("/", portal.identity_email()),
        },
    )
}

// ── GET /activation/{order_id} — early-arrival polling shell ─────────────────

pub async fn activation(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Path(order_id): Path<String>,
    Query(q): Query<ProgressQuery>,
) -> Response {
    if let Err(r) = crate::deps::require_session(&portal, "/") {
        return r;
    }
    let Some(sig) = state.signup_store.get(&q.session) else {
        return (StatusCode::NOT_FOUND, "Unknown or expired session.").into_response();
    };
    if let Some(sub) = &sig.subscription_id {
        return Redirect::to(&format!("/confirmation/{sub}?session={}", q.session)).into_response();
    }
    render(
        &state,
        "activation.html",
        context! {
            session_id => q.session,
            order_id => order_id,
            plan_id => sig.plan,
            request => request_ctx("/", portal.identity_email()),
        },
    )
}

pub async fn activation_status(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Path(order_id): Path<String>,
    Query(q): Query<ProgressQuery>,
) -> Response {
    if let Err(r) = crate::deps::require_session(&portal, "/") {
        return r;
    }
    let Some(sig) = state.signup_store.get(&q.session) else {
        return (StatusCode::NOT_FOUND, "Unknown or expired session.").into_response();
    };
    if let Some(sub) = &sig.subscription_id {
        return hx_redirect(&format!("/confirmation/{sub}?session={}", q.session));
    }
    // Still running — read fresh COM state for the stepper (best-effort).
    let state_str = match &state.clients {
        Some(c) => c
            .com
            .get_order(&order_id)
            .await
            .ok()
            .and_then(|o| o.get("state").and_then(Value::as_str).map(str::to_string))
            .unwrap_or_else(|| "in_progress".to_string()),
        None => "in_progress".to_string(),
    };
    render(
        &state,
        "partials/activation_stepper.html",
        context! {
            state => state_str,
            order_id => order_id,
            session_id => q.session,
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

    #[test]
    fn tokenize_brand_last4_and_token_prefix() {
        let visa = local_tokenize("4111 1111 1111 1111").unwrap();
        assert_eq!(visa.brand, "visa");
        assert_eq!(visa.last4, "1111");
        assert!(visa.card_token.starts_with("tok_"));

        let mc = local_tokenize("5312345678901234").unwrap();
        assert_eq!(mc.brand, "mastercard");

        let amex = local_tokenize("371234567890123").unwrap();
        assert_eq!(amex.brand, "amex");

        // The `FAIL`/`DECLINE` token branches are vestigial: the numeric-only
        // guard rejects any card_number containing letters first (matches the
        // Python `digits.isdigit()` check that precedes the marker test), so a
        // letter-bearing PAN is `Err`, never a `tok_FAIL_`/`tok_DECLINE_` token.
        assert!(local_tokenize("4111FAIL11111111").is_err());
        assert!(local_tokenize("4111DECLINE111111").is_err());

        // Too short / non-numeric → Err.
        assert!(local_tokenize("4111").is_err());
        assert!(local_tokenize("nope nope nope").is_err());
    }

    #[test]
    fn extract_sub_and_activation() {
        let order = serde_json::json!({
            "state": "completed",
            "items": [{"targetSubscriptionId": "SUB-042", "activationCode": "LPA:1$x$y"}]
        });
        assert_eq!(extract_subscription_id(&order).as_deref(), Some("SUB-042"));
        assert_eq!(
            extract_activation_code(&order).as_deref(),
            Some("LPA:1$x$y")
        );
        // Top-level alias + missing.
        let top = serde_json::json!({"subscription_id": "SUB-001"});
        assert_eq!(extract_subscription_id(&top).as_deref(), Some("SUB-001"));
        assert!(extract_subscription_id(&serde_json::json!({})).is_none());
    }
}
