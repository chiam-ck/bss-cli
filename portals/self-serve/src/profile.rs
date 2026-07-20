//! `/profile/contact` — contact-details view + name/phone/address updates +
//! the cross-schema email-change flow. Port of `bss_self_serve.routes.profile`.
//!
//! Every mutating route is `requires_linked_customer` + step-up-gated (except the
//! email *verify*, where the OTP itself is the step-up, and *cancel*, which is
//! non-destructive). One `bss-clients` write per route; a `portal_action` row on
//! success and failure. The email change commits CRM + portal_auth atomically
//! (the documented cross-schema exception).

use axum::extract::{Query, RawForm, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Redirect, Response};
use axum::Extension;
use minijinja::context;
use serde_json::Value;

use bss_clients::ClientError;
use bss_portal_auth::{
    cancel_pending_email_change, record_portal_action, start_email_change, verify_email_change,
    PortalActionRecord, StartOutcome, VerifyChangeOutcome,
};

use crate::deps::require_linked_customer;
use crate::error_messages::{is_known, render as render_rule};
use crate::middleware::PortalSession;
use crate::routes::render;
use crate::stepup::check_step_up;
use crate::templating::request_ctx;
use crate::AppState;

const OWNERSHIP_RULE: &str = "policy.customer.contact_medium.unknown";

/// Parse an `application/x-www-form-urlencoded` body into ordered key/value pairs.
pub(crate) fn parse_form(bytes: &[u8]) -> Vec<(String, String)> {
    let s = String::from_utf8_lossy(bytes);
    s.split('&')
        .filter(|p| !p.is_empty())
        .map(|pair| {
            let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
            (urldecode(k), urldecode(v))
        })
        .collect()
}

/// Minimal `application/x-www-form-urlencoded` value decode (`+`→space + `%XX`).
fn urldecode(s: &str) -> String {
    let s = s.replace('+', " ");
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(b) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                out.push(b);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

pub(crate) fn field<'a>(form: &'a [(String, String)], key: &str) -> Option<&'a str> {
    form.iter().find(|(k, _)| k == key).map(|(_, v)| v.as_str())
}

pub(crate) fn user_agent(headers: &HeaderMap) -> Option<String> {
    headers
        .get(axum::http::header::USER_AGENT)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string)
}

/// One `portal_action` row (step-up routes pass `step_up_consumed = true`).
#[allow(clippy::too_many_arguments)]
pub(crate) async fn audit(
    state: &AppState,
    customer_id: &str,
    identity_id: &str,
    action: &str,
    route: &str,
    success: bool,
    error_rule: Option<&str>,
    step_up_consumed: bool,
    ua: Option<&str>,
) {
    let Some(pool) = &state.db else { return };
    let rec = PortalActionRecord {
        customer_id: Some(customer_id),
        identity_id: Some(identity_id),
        action,
        route,
        method: "POST",
        success,
        error_rule,
        step_up_consumed,
        ip: None,
        user_agent: ua,
    };
    if let Err(e) = record_portal_action(pool, &rec).await {
        tracing::warn!(action = action, error = %e, "portal.profile.audit_failed");
    }
}

// ── GET /profile/contact ─────────────────────────────────────────────────────

pub async fn contact_view(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    Query(q): Query<FlashQuery>,
) -> Response {
    let customer_id = match require_linked_customer(&portal, "/profile/contact") {
        Ok(c) => c,
        Err(r) => return r,
    };
    render_contact(
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
/// `Redirect::to(".../?flash=...")` writes in this module).
#[derive(serde::Deserialize)]
pub struct FlashQuery {
    #[serde(default)]
    flash: Option<String>,
}

/// Render `profile_contact.html` (shared by the view + the error re-renders).
async fn render_contact(
    state: &AppState,
    portal: &PortalSession,
    customer_id: &str,
    error: Option<&str>,
    flash: Option<&str>,
    status: StatusCode,
) -> Response {
    let (mediums, individual) = match &state.clients {
        Some(c) => {
            let mediums = c
                .crm
                .list_contact_mediums(customer_id)
                .await
                .unwrap_or(Value::Array(Vec::new()));
            let individual = c
                .crm
                .get_customer(customer_id)
                .await
                .ok()
                .and_then(|cust| cust.get("individual").cloned())
                .map(|ind| {
                    serde_json::json!({
                        "given_name": ind.get("givenName").and_then(Value::as_str).unwrap_or(""),
                        "family_name": ind.get("familyName").and_then(Value::as_str).unwrap_or(""),
                    })
                })
                .unwrap_or_else(|| serde_json::json!({"given_name": "", "family_name": ""}));
            (mediums, individual)
        }
        None => (Value::Array(Vec::new()), serde_json::json!({})),
    };

    // Pending email change, read straight off portal_auth.
    let pending = pending_email_change(state, portal).await;

    let mut resp = render(
        state,
        "profile_contact.html",
        context! {
            mediums => mediums,
            individual => individual,
            pending_email_change => pending,
            error => error,
            flash => flash,
            request => request_ctx("/profile/contact", portal.identity_email()),
        },
    );
    *resp.status_mut() = status;
    resp
}

async fn pending_email_change(state: &AppState, portal: &PortalSession) -> Value {
    let (Some(pool), Some(identity)) = (&state.db, &portal.identity) else {
        return Value::Null;
    };
    let row = sqlx::query(
        "SELECT new_email, expires_at FROM portal_auth.email_change_pending \
         WHERE identity_id = $1 AND status = 'pending'",
    )
    .bind(&identity.id)
    .fetch_optional(pool)
    .await
    .ok()
    .flatten();
    match row {
        Some(r) => {
            use sqlx::Row;
            let new_email: String = r.get("new_email");
            let expires: chrono::DateTime<chrono::Utc> = r.get("expires_at");
            serde_json::json!({ "new_email": new_email, "expires_at": expires.to_rfc3339() })
        }
        None => Value::Null,
    }
}

// ── POST /profile/contact/name/update ────────────────────────────────────────

pub async fn name_update(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    headers: HeaderMap,
    RawForm(body): RawForm,
) -> Response {
    let form = parse_form(&body);
    let customer_id = match require_linked_customer(&portal, "/profile/contact") {
        Ok(c) => c,
        Err(r) => return r,
    };
    if let Err(r) = check_step_up(
        &state,
        &portal,
        "name_update",
        &headers,
        &form,
        "/profile/contact/name/update",
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
    let ua = user_agent(&headers);

    let given = field(&form, "given_name").unwrap_or("").trim();
    let family = field(&form, "family_name").unwrap_or("").trim();
    if given.is_empty() || family.is_empty() {
        return (StatusCode::UNPROCESSABLE_ENTITY, "Name required").into_response();
    }

    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };
    match clients
        .crm
        .update_individual(&customer_id, Some(given), Some(family))
        .await
    {
        Ok(_) => {
            audit(
                &state,
                &customer_id,
                &identity_id,
                "name_update",
                "/profile/contact/name/update",
                true,
                None,
                true,
                ua.as_deref(),
            )
            .await;
            Redirect::to("/profile/contact?flash=name_update").into_response()
        }
        Err(ClientError::Policy(pv)) => {
            audit(
                &state,
                &customer_id,
                &identity_id,
                "name_update",
                "/profile/contact/name/update",
                false,
                Some(&pv.rule),
                true,
                ua.as_deref(),
            )
            .await;
            if !is_known(&pv.rule) {
                tracing::info!(rule = %pv.rule, action = "name_update", "portal.profile.unknown_policy_rule");
            }
            render_contact(
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
            tracing::error!(error = %e, "portal.profile.name_update_failed");
            (StatusCode::INTERNAL_SERVER_ERROR, "Update failed").into_response()
        }
    }
}

// ── POST /profile/contact/{phone,address}/update ─────────────────────────────

pub async fn phone_update(
    state: State<AppState>,
    portal: Extension<PortalSession>,
    headers: HeaderMap,
    form: RawForm,
) -> Response {
    direct_medium_update(
        state,
        portal,
        headers,
        form,
        "phone_update",
        "mobile",
        "/profile/contact/phone/update",
    )
    .await
}

pub async fn address_update(
    state: State<AppState>,
    portal: Extension<PortalSession>,
    headers: HeaderMap,
    form: RawForm,
) -> Response {
    direct_medium_update(
        state,
        portal,
        headers,
        form,
        "address_update",
        "postal",
        "/profile/contact/address/update",
    )
    .await
}

#[allow(clippy::too_many_arguments)]
async fn direct_medium_update(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    headers: HeaderMap,
    RawForm(body): RawForm,
    action: &str,
    expected_type: &str,
    route: &str,
) -> Response {
    let form = parse_form(&body);
    let customer_id = match require_linked_customer(&portal, "/profile/contact") {
        Ok(c) => c,
        Err(r) => return r,
    };
    if let Err(r) = check_step_up(&state, &portal, action, &headers, &form, route).await {
        return r;
    }
    let identity_id = portal
        .identity
        .as_ref()
        .map(|i| i.id.clone())
        .unwrap_or_default();
    let ua = user_agent(&headers);
    let cm_id = field(&form, "cm_id").unwrap_or("").to_string();
    let value = field(&form, "value").unwrap_or("").trim().to_string();
    if cm_id.is_empty() || value.is_empty() {
        return (StatusCode::UNPROCESSABLE_ENTITY, "Missing field").into_response();
    }

    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };

    // Ownership + type check against the customer's active mediums.
    let owned = clients
        .crm
        .list_contact_mediums(&customer_id)
        .await
        .ok()
        .and_then(|v| v.as_array().cloned())
        .unwrap_or_default()
        .into_iter()
        .find(|m| m.get("id").and_then(Value::as_str) == Some(cm_id.as_str()));
    let type_ok = owned
        .as_ref()
        .and_then(|m| m.get("mediumType").and_then(Value::as_str))
        == Some(expected_type);
    if owned.is_none() || !type_ok {
        audit(
            &state,
            &customer_id,
            &identity_id,
            action,
            route,
            false,
            Some(OWNERSHIP_RULE),
            true,
            ua.as_deref(),
        )
        .await;
        let mut resp = render(
            &state,
            "profile_forbidden.html",
            context! {
                customer_facing => render_rule(OWNERSHIP_RULE),
                request => request_ctx("/profile/contact", portal.identity_email()),
            },
        );
        *resp.status_mut() = StatusCode::FORBIDDEN;
        return resp;
    }

    match clients
        .crm
        .update_contact_medium(&customer_id, &cm_id, &value)
        .await
    {
        Ok(_) => {
            audit(
                &state,
                &customer_id,
                &identity_id,
                action,
                route,
                true,
                None,
                true,
                ua.as_deref(),
            )
            .await;
            Redirect::to(&format!("/profile/contact?flash={action}")).into_response()
        }
        Err(ClientError::Policy(pv)) => {
            audit(
                &state,
                &customer_id,
                &identity_id,
                action,
                route,
                false,
                Some(&pv.rule),
                true,
                ua.as_deref(),
            )
            .await;
            if !is_known(&pv.rule) {
                tracing::info!(rule = %pv.rule, action = action, "portal.profile.unknown_policy_rule");
            }
            render_contact(
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
            tracing::error!(error = %e, "portal.profile.medium_update_failed");
            (StatusCode::INTERNAL_SERVER_ERROR, "Update failed").into_response()
        }
    }
}

// ── POST /profile/contact/email/change — start ───────────────────────────────

pub async fn email_change_start(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    headers: HeaderMap,
    RawForm(body): RawForm,
) -> Response {
    let form = parse_form(&body);
    let customer_id = match require_linked_customer(&portal, "/profile/contact") {
        Ok(c) => c,
        Err(r) => return r,
    };
    if let Err(r) = check_step_up(
        &state,
        &portal,
        "email_change",
        &headers,
        &form,
        "/profile/contact/email/change",
    )
    .await
    {
        return r;
    }
    let (Some(pool), Some(adapter), Some(identity)) =
        (&state.db, &state.email_adapter, &portal.identity)
    else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };
    let ua = user_agent(&headers);
    let new_email = field(&form, "new_email").unwrap_or("");
    if new_email.len() < 3 {
        return (StatusCode::UNPROCESSABLE_ENTITY, "Email required").into_response();
    }

    match start_email_change(
        pool,
        &identity.id,
        new_email,
        None,
        ua.as_deref(),
        adapter.as_ref(),
    )
    .await
    {
        Ok(StartOutcome::Started(started)) => {
            audit(
                &state,
                &customer_id,
                &identity.id,
                "email_change",
                "/profile/contact/email/change",
                true,
                None,
                true,
                ua.as_deref(),
            )
            .await;
            render(
                &state,
                "profile_email_pending.html",
                context! {
                    new_email => started.new_email,
                    request => request_ctx("/profile/contact", portal.identity_email()),
                },
            )
        }
        Ok(StartOutcome::Failed(f)) => {
            let rule = format!("policy.customer.contact_medium.{}", f.reason);
            audit(
                &state,
                &customer_id,
                &identity.id,
                "email_change",
                "/profile/contact/email/change",
                false,
                Some(&rule),
                true,
                ua.as_deref(),
            )
            .await;
            render_contact(
                &state,
                &portal,
                &customer_id,
                Some(render_rule(&rule)),
                None,
                StatusCode::UNPROCESSABLE_ENTITY,
            )
            .await
        }
        Err(e) => {
            tracing::error!(error = %e, "portal.profile.email_change_start_failed");
            (StatusCode::INTERNAL_SERVER_ERROR, "Failed").into_response()
        }
    }
}

// ── GET /profile/contact/email/verify — form ─────────────────────────────────

pub async fn email_change_verify_form(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
) -> Response {
    if let Err(r) = require_linked_customer(&portal, "/profile/contact") {
        return r;
    }
    render(
        &state,
        "profile_email_verify.html",
        context! {
            error => Option::<String>::None,
            request => request_ctx("/profile/contact", portal.identity_email()),
        },
    )
}

// ── POST /profile/contact/email/verify — atomic commit ───────────────────────

pub async fn email_change_verify_submit(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    headers: HeaderMap,
    RawForm(body): RawForm,
) -> Response {
    let form = parse_form(&body);
    // The OTP is the step-up here — only a session is required.
    let customer_id = match require_linked_customer(&portal, "/profile/contact") {
        Ok(c) => c,
        Err(r) => return r,
    };
    let (Some(pool), Some(identity)) = (&state.db, &portal.identity) else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };
    let ua = user_agent(&headers);
    let code = field(&form, "code").unwrap_or("");
    if code.len() < 4 {
        return (StatusCode::BAD_REQUEST, "Code required").into_response();
    }

    match verify_email_change(pool, &identity.id, code).await {
        Ok(VerifyChangeOutcome::Applied(_)) => {
            audit(
                &state,
                &customer_id,
                &identity.id,
                "email_change",
                "/profile/contact/email/verify",
                true,
                None,
                false,
                ua.as_deref(),
            )
            .await;
            Redirect::to("/profile/contact?flash=email_change").into_response()
        }
        Ok(VerifyChangeOutcome::Failed(f)) => {
            let rule = format!("policy.customer.contact_medium.{}", f.reason);
            audit(
                &state,
                &customer_id,
                &identity.id,
                "email_change",
                "/profile/contact/email/verify",
                false,
                Some(&rule),
                false,
                ua.as_deref(),
            )
            .await;
            let mut resp = render(
                &state,
                "profile_email_verify.html",
                context! {
                    error => render_rule(&rule),
                    request => request_ctx("/profile/contact", portal.identity_email()),
                },
            );
            *resp.status_mut() = StatusCode::BAD_REQUEST;
            resp
        }
        Err(e) => {
            tracing::error!(error = %e, "portal.profile.email_verify_failed");
            let rule = "policy.customer.contact_medium.unknown";
            audit(
                &state,
                &customer_id,
                &identity.id,
                "email_change",
                "/profile/contact/email/verify",
                false,
                Some(rule),
                false,
                ua.as_deref(),
            )
            .await;
            let mut resp = render(
                &state,
                "profile_email_verify.html",
                context! {
                    error => render_rule(rule),
                    request => request_ctx("/profile/contact", portal.identity_email()),
                },
            );
            *resp.status_mut() = StatusCode::INTERNAL_SERVER_ERROR;
            resp
        }
    }
}

// ── POST /profile/contact/email/cancel ───────────────────────────────────────

pub async fn email_change_cancel(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
    headers: HeaderMap,
) -> Response {
    let customer_id = match require_linked_customer(&portal, "/profile/contact") {
        Ok(c) => c,
        Err(r) => return r,
    };
    let (Some(pool), Some(identity)) = (&state.db, &portal.identity) else {
        return (StatusCode::SERVICE_UNAVAILABLE, "Unavailable").into_response();
    };
    let ua = user_agent(&headers);
    let cancelled = cancel_pending_email_change(pool, &identity.id)
        .await
        .unwrap_or(false);
    let rule = if cancelled {
        None
    } else {
        Some("policy.customer.contact_medium.no_active_pending")
    };
    audit(
        &state,
        &customer_id,
        &identity.id,
        "email_change",
        "/profile/contact/email/cancel",
        cancelled,
        rule,
        false,
        ua.as_deref(),
    )
    .await;
    Redirect::to("/profile/contact?flash=email_change_cancelled").into_response()
}
