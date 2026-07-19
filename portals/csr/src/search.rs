//! `GET /search` — find a customer by name / MSISDN / email, then jump into a
//! cockpit session pinned to them. Port of `bss_csr.routes.search`.
//!
//! Lighter than the v0.5 search route: it returns a card list, not a 360 view.
//! Each card's "Start cockpit session" button POSTs `/search/start_session`, which
//! opens a fresh conversation with the customer pre-pinned and 303s onto it.
//!
//! MSISDN-shaped queries do NOT 303 to a 360 (that page is gone in v0.13); they
//! resolve through `crm.find_customer_by_msisdn` to a single card, same as the
//! name-search flow.

use axum::extract::{Form, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Redirect, Response};
use bss_cockpit::OPERATOR_ACTOR;
use serde::Deserialize;
use serde_json::Value;

use crate::routes::render;
use crate::views::flatten_customer;
use crate::AppState;

/// `^\+?\d{6,}$` — an all-digits (optionally `+`-led) query is an MSISDN.
fn looks_like_msisdn(q: &str) -> bool {
    let digits = q.strip_prefix('+').unwrap_or(q);
    digits.len() >= 6 && digits.chars().all(|c| c.is_ascii_digit())
}

/// `^[^@\s]+@[^@\s]+$` — one `@` with non-space text either side. Deliberately
/// loose; the CRM lookup is the real validator, the predicate only routes.
fn looks_like_email(q: &str) -> bool {
    let mut parts = q.split('@');
    match (parts.next(), parts.next(), parts.next()) {
        (Some(local), Some(domain), None) => {
            !local.is_empty()
                && !domain.is_empty()
                && !local.contains(char::is_whitespace)
                && !domain.contains(char::is_whitespace)
        }
        _ => false,
    }
}

#[derive(Deserialize, Default)]
pub struct SearchQuery {
    #[serde(default)]
    q: String,
}

fn found(cust: Value) -> Vec<Value> {
    // Python guards on truthiness — null / `{}` is "not found".
    if cust.is_null() || cust.as_object().is_some_and(|o| o.is_empty()) {
        Vec::new()
    } else {
        vec![flatten_customer(&cust)]
    }
}

/// `GET /search`.
pub async fn search(State(state): State<AppState>, Query(q): Query<SearchQuery>) -> Response {
    let q_clean = q.q.trim().to_string();
    let mut results: Vec<Value> = Vec::new();

    if let Some(clients) = &state.clients {
        if !q_clean.is_empty() && looks_like_msisdn(&q_clean) {
            let digits = q_clean.trim_start_matches('+').replace(' ', "");
            if let Ok(cust) = clients.crm.find_customer_by_msisdn(&digits).await {
                results = found(cust);
            }
        } else if !q_clean.is_empty() && looks_like_email(&q_clean) {
            if let Ok(cust) = clients.crm.find_customer_by_email(&q_clean).await {
                results = found(cust);
            }
        } else if !q_clean.is_empty() {
            if let Ok(v) = clients.crm.list_customers(None, Some(&q_clean)).await {
                results = v
                    .as_array()
                    .map(|arr| arr.iter().map(flatten_customer).collect())
                    .unwrap_or_default();
            }
        }
    }

    render(
        &state,
        "search.html",
        minijinja::Value::from_serialize(serde_json::json!({
            "active_page": "search",
            "model": "(env default)",
            "q": q_clean,
            "results": results,
        })),
    )
}

#[derive(Deserialize)]
pub struct StartSessionForm {
    customer_id: String,
}

/// `POST /search/start_session` — open a fresh cockpit session pinned to a
/// customer; 303 onto it. Mirrors `/cockpit/new` but sets the focus at creation
/// so the operator lands on the thread already pinned.
pub async fn start_session(
    State(state): State<AppState>,
    Form(form): Form<StartSessionForm>,
) -> Response {
    let s = match crate::cockpit::store(&state) {
        Ok(s) => s,
        Err(r) => return r,
    };
    let customer_id = form.customer_id;
    // Python slices the label to 32 chars; the focus carries the full id.
    let label_safe: String = customer_id.chars().take(32).collect();
    let label = format!("customer {label_safe}");
    match s
        .open(
            OPERATOR_ACTOR,
            Some(&label),
            Some(&customer_id),
            false,
            &crate::cockpit::tenant(),
        )
        .await
    {
        Ok(conv) => Redirect::to(&format!("/cockpit/{}", conv.session_id)).into_response(),
        Err(e) => {
            tracing::error!(error = %e, "csr.search.start_session_failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "could not open a session",
            )
                .into_response()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn msisdn_predicate() {
        assert!(looks_like_msisdn("6591110001"));
        assert!(looks_like_msisdn("+6591110001"));
        assert!(!looks_like_msisdn("12345"));
        assert!(!looks_like_msisdn("ada@example.com"));
    }

    #[test]
    fn email_predicate() {
        assert!(looks_like_email("ada@example.com"));
        assert!(looks_like_email("a@b"));
        // Two @, spaces, or a missing side all fail.
        assert!(!looks_like_email("a@b@c"));
        assert!(!looks_like_email("ada @ example.com"));
        assert!(!looks_like_email("@example.com"));
        assert!(!looks_like_email("ada@"));
        assert!(!looks_like_email("plainname"));
    }
}
