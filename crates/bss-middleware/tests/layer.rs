//! Middleware tests — port of `test_token_auth.py`. Exercises the token layer
//! composed with `bss_context::propagate_context`, so the resolved identity
//! reaches a handler exactly as the two Python middlewares deliver it.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;
use std::sync::Arc;

use axum::{
    body::{to_bytes, Body},
    extract::Request,
    http::StatusCode,
    middleware::{from_fn, from_fn_with_state},
    routing::{get, post},
    Extension, Router,
};
use bss_context::{propagate_context, RequestCtx};
use bss_middleware::{
    load_token_map, require_api_token, AUTH_INVALID_TOKEN, AUTH_MISSING_TOKEN, TEST_TOKEN,
};
use serde_json::Value;
use tower::ServiceExt;

const PORTAL_TOKEN: &str = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789";

async fn root() -> &'static str {
    "ok"
}
async fn health() -> &'static str {
    "ok"
}
async fn webhook() -> &'static str {
    "ok"
}
/// Exposes the resolved identity the same way the Python test app did
/// (`auth_context.current().service_identity`) — here via the context layer.
async fn whoami(Extension(ctx): Extension<RequestCtx>) -> String {
    ctx.service_identity
}

fn app() -> Router {
    let map = Arc::new(load_token_map(&BTreeMap::from([
        ("BSS_API_TOKEN".to_string(), TEST_TOKEN.to_string()),
        (
            "BSS_PORTAL_SELF_SERVE_API_TOKEN".to_string(),
            PORTAL_TOKEN.to_string(),
        ),
    ])));
    Router::new()
        .route("/", get(root))
        .route("/whoami", get(whoami))
        .route("/health", get(health))
        .route("/health/ready", get(health))
        .route("/healthz", get(health))
        .route("/webhooks/resend", post(webhook))
        .route("/webhooks/stripe", post(webhook))
        .route("/webhooks", post(webhook))
        // inner: context layer reads the ServiceIdentity the token layer sets.
        .layer(from_fn(propagate_context))
        // outer: perimeter token gate.
        .layer(from_fn_with_state(map, require_api_token))
}

async fn call(method: &str, uri: &str, token: Option<&str>) -> (StatusCode, Vec<u8>) {
    let mut b = Request::builder().method(method).uri(uri);
    if let Some(t) = token {
        b = b.header("X-BSS-API-Token", t);
    }
    let resp = app().oneshot(b.body(Body::empty()).unwrap()).await.unwrap();
    let status = resp.status();
    let body = to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap()
        .to_vec();
    (status, body)
}

// ─── exemptions ─────────────────────────────────────────────────────────────

#[tokio::test]
async fn health_paths_exempt() {
    assert_eq!(call("GET", "/health", None).await.0, StatusCode::OK);
    assert_eq!(call("GET", "/health/ready", None).await.0, StatusCode::OK);
}

#[tokio::test]
async fn healthz_not_exempt() {
    assert_eq!(
        call("GET", "/healthz", None).await.0,
        StatusCode::UNAUTHORIZED
    );
}

#[tokio::test]
async fn webhooks_provider_paths_exempt_but_bare_is_not() {
    assert_eq!(
        call("POST", "/webhooks/resend", None).await.0,
        StatusCode::OK
    );
    assert_eq!(
        call("POST", "/webhooks/stripe", None).await.0,
        StatusCode::OK
    );
    // bare /webhooks (no provider segment) must NOT be exempt.
    assert_eq!(
        call("POST", "/webhooks", None).await.0,
        StatusCode::UNAUTHORIZED
    );
}

// ─── token gate ─────────────────────────────────────────────────────────────

#[tokio::test]
async fn missing_token_401() {
    let (status, body) = call("GET", "/", None).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    let v: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(v["code"], AUTH_MISSING_TOKEN);
    assert!(v["message"].as_str().unwrap().contains("X-BSS-API-Token"));
}

#[tokio::test]
async fn wrong_token_401_and_no_echo() {
    let (status, body) = call("GET", "/", Some("wrong-token-value")).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    let text = String::from_utf8(body).unwrap();
    let v: Value = serde_json::from_str(&text).unwrap();
    assert_eq!(v["code"], AUTH_INVALID_TOKEN);
    assert!(!text.contains("wrong-token-value"), "token echoed in body");
}

#[tokio::test]
async fn correct_token_200() {
    let (status, body) = call("GET", "/", Some(TEST_TOKEN)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, b"ok");
}

#[tokio::test]
async fn header_name_case_insensitive() {
    // HeaderMap lowercases names; send a mixed-case name explicitly.
    let map = Arc::new(load_token_map(&BTreeMap::from([(
        "BSS_API_TOKEN".to_string(),
        TEST_TOKEN.to_string(),
    )])));
    let app = Router::new()
        .route("/", get(root))
        .layer(from_fn_with_state(map, require_api_token));
    let req = Request::builder()
        .uri("/")
        .header("X-BSS-Api-Token", TEST_TOKEN)
        .body(Body::empty())
        .unwrap();
    assert_eq!(app.oneshot(req).await.unwrap().status(), StatusCode::OK);
}

// ─── service_identity (v0.9) ────────────────────────────────────────────────

#[tokio::test]
async fn identity_attached_from_token_not_header() {
    // default token → "default"
    let (status, body) = call("GET", "/whoami", Some(TEST_TOKEN)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(String::from_utf8(body).unwrap(), "default");

    // named token → "portal_self_serve"
    let (_, body) = call("GET", "/whoami", Some(PORTAL_TOKEN)).await;
    assert_eq!(String::from_utf8(body).unwrap(), "portal_self_serve");
}

#[tokio::test]
async fn spoofed_identity_header_ignored() {
    // Caller cannot assert their own identity via a header — it comes from the
    // token map only. Send a bogus X-BSS-Service-Identity and a default token.
    let req = Request::builder()
        .uri("/whoami")
        .header("X-BSS-API-Token", TEST_TOKEN)
        .header("X-BSS-Service-Identity", "operator_cockpit")
        .body(Body::empty())
        .unwrap();
    let resp = app().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    assert_eq!(
        &body[..],
        b"default",
        "identity must come from the token, not a header"
    );
}
