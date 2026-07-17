//! Customer screens — list/search + the 360 detail (v1.6 cockpit CRM). Port of
//! `bss_csr.routes.customers`.
//!
//! Reads go direct through the portal's own [`CockpitClients`](crate::clients::CockpitClients).
//! Writes are single policy-gated `bss-clients` calls: interactions, cases, name +
//! contact-medium CRUD, and (v1.6.1, operator directive) `customer.close` /
//! `remove_contact_medium` as direct CRUD **behind the two-step UI confirm** — the
//! expanded danger panel posts `confirm=yes` and the route refuses without it. The
//! human click through the consequence text is the authorisation; the policy layer
//! stays the server gate.
//!
//! **Doctrine:** one CRM-screen POST → one `bss-clients` write. Compound work goes
//! to the agent via the "Ask the agent" handoff, never composed here.

use axum::extract::{Form, Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Redirect, Response};
use bss_clients::ClientError;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::routes::render;
use crate::views::{
    balance_rows, customer_name, field, field_str, flatten_case, flatten_customer, flatten_order,
    fmt_dt,
};
use crate::AppState;

const PAGE_SIZE: i64 = 25;

/// `^\+?\d{6,}$` — an all-digits query (optionally `+`-led) is an MSISDN lookup,
/// not a name search. Hand-rolled: the crate's `regex` would pull the same answer
/// for more cost, and this predicate is exact.
fn looks_like_msisdn(q: &str) -> bool {
    let digits = q.strip_prefix('+').unwrap_or(q);
    digits.len() >= 6 && !digits.is_empty() && digits.chars().all(|c| c.is_ascii_digit())
}

const CUSTOMER_STATES: [&str; 3] = ["active", "suspended", "closed"];

/// The message the danger panel's absence earns. Pinned by the confirm-gate test.
pub const CONFIRM_REQUIRED: &str = "This action needs the expanded confirm step.";

#[derive(Deserialize, Default)]
pub struct ListQuery {
    #[serde(default)]
    q: String,
    #[serde(default)]
    state: String,
    #[serde(default)]
    page: i64,
}

/// `GET /customers`.
pub async fn customers_list(State(state): State<AppState>, Query(q): Query<ListQuery>) -> Response {
    let q_clean = q.q.trim().to_string();
    let state_clean = q.state.trim().to_string();
    // FastAPI's `Query(ge=0, le=10_000)` rejects out-of-range with a 422; axum has
    // no declarative equivalent, so clamp-and-422 explicitly rather than letting a
    // negative offset reach the service.
    if !(0..=10_000).contains(&q.page) {
        return (StatusCode::UNPROCESSABLE_ENTITY, "page out of range").into_response();
    }

    let mut rows: Vec<Value> = Vec::new();
    let mut has_next = false;

    if let Some(clients) = &state.clients {
        if !q_clean.is_empty() && looks_like_msisdn(&q_clean) {
            let digits = q_clean.trim_start_matches('+').replace(' ', "");
            if let Ok(cust) = clients.crm.find_customer_by_msisdn(&digits).await {
                // Python guards on truthiness — a null/`{}` body finds nothing.
                if !cust.is_null() && cust.as_object().is_some_and(|o| !o.is_empty()) {
                    rows = vec![flatten_customer(&cust)];
                }
            }
        } else {
            // Fetch one extra row to know whether a next page exists.
            let raw = match clients
                .crm
                .list_customers_paged(
                    opt(&state_clean),
                    opt(&q_clean),
                    Some(PAGE_SIZE + 1),
                    Some(q.page * PAGE_SIZE),
                )
                .await
            {
                Ok(v) => v.as_array().cloned().unwrap_or_default(),
                Err(e) => {
                    tracing::warn!(status = e.status_code(), "csr.customers.list_failed");
                    Vec::new()
                }
            };
            has_next = raw.len() as i64 > PAGE_SIZE;
            rows = raw
                .iter()
                .take(PAGE_SIZE as usize)
                .map(flatten_customer)
                .collect();
        }
    }

    render(
        &state,
        "customers_list.html",
        minijinja::Value::from_serialize(json!({
            "active_page": "customers",
            "model": "(env default)",
            "q": q_clean,
            "state": state_clean,
            "states": CUSTOMER_STATES,
            "rows": rows,
            "page": q.page,
            "has_prev": q.page > 0,
            "has_next": has_next,
        })),
    )
}

/// `""` → `None`, so the client omits the filter entirely.
fn opt(s: &str) -> Option<&str> {
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}

/// `(payload, ok)` — sections degrade independently so one slow/down service
/// doesn't blank the whole 360.
fn section(r: Result<Value, ClientError>) -> (Option<Value>, bool) {
    match r {
        Ok(v) => (Some(v), true),
        Err(e) => {
            tracing::warn!(error = %e, "csr.customer_360.section_failed");
            (None, false)
        }
    }
}

fn as_array(v: &Option<Value>) -> Vec<Value> {
    v.as_ref()
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

/// `GET /customers/{customer_id}`.
pub async fn customer_detail(
    State(state): State<AppState>,
    Path(customer_id): Path<String>,
    Query(q): Query<FlashQuery>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let cust = match clients.crm.get_customer(&customer_id).await {
        Ok(c) => c,
        Err(ClientError::NotFound(_)) => {
            return (
                StatusCode::NOT_FOUND,
                format!("Customer {customer_id} not found"),
            )
                .into_response()
        }
        Err(e) => {
            tracing::error!(error = %e, "csr.customer_360.get_failed");
            return (StatusCode::BAD_GATEWAY, "CRM error").into_response();
        }
    };

    // The six sections fan out concurrently, exactly as the Python `asyncio.gather`.
    let (subs, orders, cases, interactions, methods, kyc) = tokio::join!(
        clients.subscription.list_for_customer(&customer_id),
        clients.com.list_orders(Some(&customer_id)),
        clients.crm.list_cases(Some(&customer_id), None, None),
        clients.crm.list_interactions(&customer_id, 15),
        clients.payment.list_methods(&customer_id),
        clients.crm.get_kyc_status(&customer_id),
    );
    let (subs, subs_ok) = section(subs);
    let (orders, orders_ok) = section(orders);
    let (cases, cases_ok) = section(cases);
    let (interactions, interactions_ok) = section(interactions);
    let (methods, methods_ok) = section(methods);
    let (kyc, _) = section(kyc);

    let sub_views: Vec<Value> = as_array(&subs)
        .iter()
        .map(|s| {
            json!({
                "id": s.get("id").and_then(Value::as_str).unwrap_or("?"),
                "offering_id": field_str(Some(s), &["offering_id"], "—"),
                "msisdn": s.get("msisdn").and_then(Value::as_str).unwrap_or("—"),
                "state": field_str(Some(s), &["state"], "?"),
                "next_renewal": fmt_dt(&field_str(Some(s), &["next_renewal_at"], "")),
                "balances": balance_rows(s.get("balances")),
            })
        })
        .collect();

    let method_views: Vec<Value> = as_array(&methods)
        .iter()
        .map(|m| {
            let card = m
                .get("cardSummary")
                .or_else(|| m.get("card_summary"))
                .cloned()
                .unwrap_or_else(|| json!({}));
            json!({
                "id": m.get("id").and_then(Value::as_str).unwrap_or("?"),
                "brand": field_str(Some(&card), &["brand"], "card"),
                // `last4` only — never a full PAN (doctrine).
                "last4": field_str(Some(&card), &["last4", "masked_pan"], "????"),
                "exp": format!(
                    "{}/{}",
                    field_str(Some(&card), &["exp_month"], "??"),
                    field_str(Some(&card), &["exp_year"], "??"),
                ),
                "is_default": field(Some(m), &["is_default"])
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
                "status": field_str(Some(m), &["status"], ""),
            })
        })
        .collect();

    let interaction_views: Vec<Value> = as_array(&interactions)
        .iter()
        .map(|i| {
            json!({
                "at": fmt_dt(&field_str(Some(i), &["occurred_at", "created_at"], "")),
                "channel": field_str(Some(i), &["channel"], "—"),
                "direction": field_str(Some(i), &["direction"], ""),
                "summary": field_str(Some(i), &["summary", "action"], ""),
            })
        })
        .collect();

    let individual = cust.get("individual").cloned().unwrap_or_else(|| json!({}));
    let contact_mediums: Vec<Value> = cust
        .get("contactMedium")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .iter()
        .map(|cm| {
            let ch = cm.get("characteristic");
            let value = cm.get("value").and_then(Value::as_str).unwrap_or("");
            let resolved = if !value.is_empty() {
                value.to_string()
            } else {
                // Python chains `or` across both characteristic spellings.
                let email = ch
                    .and_then(|c| c.get("emailAddress"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if email.is_empty() {
                    ch.and_then(|c| c.get("phoneNumber"))
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string()
                } else {
                    email.to_string()
                }
            };
            json!({
                "id": cm.get("id").and_then(Value::as_str).unwrap_or(""),
                "type": cm.get("mediumType").and_then(Value::as_str).unwrap_or("?"),
                "value": resolved,
            })
        })
        .collect();

    render(
        &state,
        "customer_detail.html",
        minijinja::Value::from_serialize(json!({
            "active_page": "customers",
            "model": "(env default)",
            "customer": flatten_customer(&cust),
            "customer_raw_name": customer_name(Some(&cust)),
            "given_name": individual.get("givenName").and_then(Value::as_str).unwrap_or(""),
            "family_name": individual.get("familyName").and_then(Value::as_str).unwrap_or(""),
            "contact_mediums": contact_mediums,
            "kyc": kyc.unwrap_or_else(|| json!({})),
            "subscriptions": sub_views,
            "subs_ok": subs_ok,
            "orders": as_array(&orders).iter().map(flatten_order).collect::<Vec<_>>(),
            "orders_ok": orders_ok,
            "cases": as_array(&cases).iter().map(flatten_case).collect::<Vec<_>>(),
            "cases_ok": cases_ok,
            "interactions": interaction_views,
            "interactions_ok": interactions_ok,
            "payment_methods": method_views,
            "methods_ok": methods_ok,
            "flash": q.flash,
            // Python slices to 300 *characters*.
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

/// `_back_to_customer` — 303 onto the 360 with an optional flash/err.
fn back_to_customer(customer_id: &str, flash: &str, err: &str) -> Response {
    crate::routes::back_to(&format!("/customers/{customer_id}"), flash, err)
}

/// `_write` — run one customer write; flash the outcome back onto the 360.
fn write_result(customer_id: &str, action: &str, r: Result<Value, ClientError>) -> Response {
    crate::routes::write_result(&format!("/customers/{customer_id}"), action, r)
}

// ── Writes ───────────────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct InteractionForm {
    summary: String,
    #[serde(default = "inbound")]
    direction: String,
}

fn inbound() -> String {
    "inbound".to_string()
}

/// `POST /customers/{customer_id}/interaction`.
pub async fn log_interaction(
    State(state): State<AppState>,
    Path(customer_id): Path<String>,
    Form(form): Form<InteractionForm>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    // Anything but the two legal directions is coerced to `inbound`, not rejected.
    let direction = match form.direction.as_str() {
        "inbound" | "outbound" => form.direction.as_str(),
        _ => "inbound",
    };
    let r = clients
        .crm
        .log_interaction_full(
            &customer_id,
            form.summary.trim(),
            Some("portal-csr"),
            Some(direction),
            None,
        )
        .await;
    write_result(&customer_id, "interaction_logged", r)
}

#[derive(Deserialize)]
pub struct NameForm {
    given_name: String,
    family_name: String,
}

/// `POST /customers/{customer_id}/name`.
pub async fn update_name(
    State(state): State<AppState>,
    Path(customer_id): Path<String>,
    Form(form): Form<NameForm>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients
        .crm
        .update_individual(
            &customer_id,
            Some(form.given_name.trim()),
            Some(form.family_name.trim()),
        )
        .await;
    write_result(&customer_id, "name_updated", r)
}

#[derive(Deserialize)]
pub struct ContactForm {
    medium_type: String,
    value: String,
}

/// `POST /customers/{customer_id}/contact`.
pub async fn add_contact(
    State(state): State<AppState>,
    Path(customer_id): Path<String>,
    Form(form): Form<ContactForm>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    // Unknown medium types fall back to `email` rather than erroring.
    let mtype = match form.medium_type.as_str() {
        "email" | "mobile" => form.medium_type.as_str(),
        _ => "email",
    };
    let r = clients
        .crm
        .add_contact_medium(&customer_id, mtype, form.value.trim())
        .await;
    write_result(&customer_id, "contact_added", r)
}

#[derive(Deserialize)]
pub struct ContactValueForm {
    value: String,
}

/// `POST /customers/{customer_id}/contact/{medium_id}`.
pub async fn update_contact(
    State(state): State<AppState>,
    Path((customer_id, medium_id)): Path<(String, String)>,
    Form(form): Form<ContactValueForm>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients
        .crm
        .update_contact_medium(&customer_id, &medium_id, form.value.trim())
        .await;
    write_result(&customer_id, "contact_updated", r)
}

#[derive(Deserialize, Default)]
pub struct ConfirmForm {
    #[serde(default)]
    confirm: String,
}

/// `POST /customers/{customer_id}/contact/{medium_id}/remove` — **confirm-gated**.
pub async fn remove_contact(
    State(state): State<AppState>,
    Path((customer_id, medium_id)): Path<(String, String)>,
    Form(form): Form<ConfirmForm>,
) -> Response {
    if form.confirm != "yes" {
        return back_to_customer(&customer_id, "", CONFIRM_REQUIRED);
    }
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients
        .crm
        .remove_contact_medium(&customer_id, &medium_id)
        .await;
    write_result(&customer_id, "contact_removed", r)
}

/// `POST /customers/{customer_id}/close` — **confirm-gated**.
pub async fn close_customer(
    State(state): State<AppState>,
    Path(customer_id): Path<String>,
    Form(form): Form<ConfirmForm>,
) -> Response {
    if form.confirm != "yes" {
        return back_to_customer(&customer_id, "", CONFIRM_REQUIRED);
    }
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients.crm.close_customer(&customer_id).await;
    write_result(&customer_id, "customer_closed", r)
}

#[derive(Deserialize)]
pub struct CaseForm {
    subject: String,
    #[serde(default = "technical")]
    category: String,
    #[serde(default = "normal")]
    priority: String,
    #[serde(default)]
    description: String,
}

fn technical() -> String {
    "technical".to_string()
}

fn normal() -> String {
    "normal".to_string()
}

/// `POST /customers/{customer_id}/case` — opens a case, then lands on its thread.
pub async fn open_case(
    State(state): State<AppState>,
    Path(customer_id): Path<String>,
    Form(form): Form<CaseForm>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let description = form.description.trim();
    let case = clients
        .crm
        .open_case(
            &customer_id,
            form.subject.trim(),
            &form.category,
            &form.priority,
            // Python: `description.strip() or None` — blank is omitted, not sent.
            if description.is_empty() {
                None
            } else {
                Some(description)
            },
            None,
            None,
        )
        .await;
    match case {
        Ok(c) => {
            let case_id = c.get("id").and_then(Value::as_str).unwrap_or("");
            Redirect::to(&format!("/case/{case_id}?flash=case_opened")).into_response()
        }
        Err(ClientError::Policy(p)) => back_to_customer(&customer_id, "", &p.message),
        Err(e) => back_to_customer(
            &customer_id,
            "",
            &format!("CRM error ({})", e.status_code()),
        ),
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    #[test]
    fn msisdn_predicate_matches_python_regex() {
        assert!(looks_like_msisdn("6591110001"));
        assert!(looks_like_msisdn("+6591110001"));
        assert!(looks_like_msisdn("123456"));
        // Under six digits, non-digits, and a bare `+` all fall through to name search.
        assert!(!looks_like_msisdn("12345"));
        assert!(!looks_like_msisdn("Ada Tan"));
        assert!(!looks_like_msisdn("+"));
        assert!(!looks_like_msisdn(""));
        // A trailing/leading non-digit disqualifies (the regex is anchored).
        assert!(!looks_like_msisdn("6591110001x"));
        assert!(!looks_like_msisdn("65911 0001"));
    }

    #[test]
    fn back_to_customer_drops_empty_params_and_encodes() {
        let loc = |r: Response| {
            r.headers()
                .get("location")
                .unwrap()
                .to_str()
                .unwrap()
                .to_string()
        };
        assert_eq!(loc(back_to_customer("CUST-1", "", "")), "/customers/CUST-1");
        assert_eq!(
            loc(back_to_customer("CUST-1", "name_updated", "")),
            "/customers/CUST-1?flash=name_updated"
        );
        // The policy message is operator-facing prose — it must survive the round trip.
        let r = back_to_customer("CUST-1", "", "Case CASE-042 has 2 open tickets.");
        assert_eq!(
            loc(r),
            "/customers/CUST-1?err=Case+CASE-042+has+2+open+tickets."
        );
    }
}
