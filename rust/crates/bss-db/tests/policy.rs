//! PolicyViolation wire-contract tests. The 422 body is what the LLM reads, so
//! the exact keys/values are pinned here. Mirrors the Python raise-side
//! (`policies/base`), the wire serialization (`RequestIdMiddleware`), and the
//! client parse (`bss_clients.base._handle_response`).
#![allow(clippy::unwrap_used, clippy::expect_used)]

use axum::{
    body::{to_bytes, Body},
    http::{Request, StatusCode},
    routing::get,
    Router,
};
use bss_db::PolicyViolation;
use serde_json::{json, Value};
use tower::ServiceExt;

#[test]
fn wire_shape_is_exact() {
    let pv = PolicyViolation::with_context(
        "case.close.requires_all_tickets_resolved",
        "Case CASE-042 has 2 open tickets (TKT-101, TKT-103). Resolve them first.",
        json!({"case_id": "CASE-042", "open_tickets": ["TKT-101", "TKT-103"]}),
    );
    let wire = pv.to_wire();
    // Exactly these five keys, no more.
    let obj = wire.as_object().unwrap();
    let mut keys: Vec<&str> = obj.keys().map(String::as_str).collect();
    keys.sort();
    assert_eq!(
        keys,
        ["code", "context", "message", "reason", "referenceError"]
    );
    assert_eq!(wire["code"], "POLICY_VIOLATION");
    assert_eq!(wire["reason"], "case.close.requires_all_tickets_resolved"); // rule → reason
    assert_eq!(
        wire["message"],
        "Case CASE-042 has 2 open tickets (TKT-101, TKT-103). Resolve them first."
    );
    assert_eq!(
        wire["referenceError"],
        "https://docs.bss-cli.dev/policies/case.close.requires_all_tickets_resolved"
    );
    assert_eq!(wire["context"]["case_id"], "CASE-042");
}

#[test]
fn empty_context_defaults_to_object() {
    let pv = PolicyViolation::new("customer.email.unique", "Email already in use");
    assert_eq!(pv.to_wire()["context"], json!({}));
}

#[test]
fn round_trips_through_client_parse() {
    let pv = PolicyViolation::with_context(
        "subscription.renew.requires_active_cof",
        "no card on file",
        json!({"subscription_id": "SUB-007"}),
    );
    // to_wire (server) → from_wire (client) reproduces the same violation.
    let parsed = PolicyViolation::from_wire(&pv.to_wire()).unwrap();
    assert_eq!(parsed, pv);
}

#[test]
fn from_wire_rejects_non_policy_bodies() {
    assert!(PolicyViolation::from_wire(&json!({"code": "NOT_FOUND"})).is_none());
    assert!(PolicyViolation::from_wire(&json!({"reason": "x"})).is_none()); // no code
                                                                            // Missing required message → None.
    assert!(
        PolicyViolation::from_wire(&json!({"code": "POLICY_VIOLATION", "reason": "r"})).is_none()
    );
}

#[test]
fn from_wire_tolerates_absent_context() {
    let body = json!({"code": "POLICY_VIOLATION", "reason": "r", "message": "m"});
    let pv = PolicyViolation::from_wire(&body).unwrap();
    assert_eq!(pv.context, json!({}));
}

#[test]
fn display_is_the_message() {
    let pv = PolicyViolation::new("rule.id", "the human message");
    assert_eq!(pv.to_string(), "the human message");
}

#[tokio::test]
async fn into_response_is_422_with_body() {
    async fn handler() -> Result<&'static str, PolicyViolation> {
        Err(PolicyViolation::new(
            "order.submit.requires_payment_method",
            "no payment method",
        ))
    }
    let app = Router::new().route("/x", get(handler));
    let resp = app
        .oneshot(Request::builder().uri("/x").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
    let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let v: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(v["code"], "POLICY_VIOLATION");
    assert_eq!(v["reason"], "order.submit.requires_payment_method");
}
