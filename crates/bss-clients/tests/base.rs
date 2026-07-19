//! BssClient base tests — port of `test_base_errors.py`, `test_auth_provider.py`,
//! and `test_header_propagation.py`. Uses a real local axum server as the peer
//! (the respx equivalent), so the reqwest path is exercised end to end.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use axum::{
    extract::State,
    http::{HeaderMap, StatusCode},
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use bss_clients::{
    AuthProvider, BssClient, ClientError, NamedTokenAuthProvider, NoAuthProvider, TokenAuthProvider,
};
use bss_context::{scope, RequestCtx};
use reqwest::Method;
use serde_json::{json, Value};

#[derive(Clone, Default)]
struct AppState {
    flaky_calls: Arc<AtomicUsize>,
}

/// Spawn a local peer server; returns its base URL.
async fn spawn_peer() -> String {
    let state = AppState::default();
    let app = Router::new()
        .route(
            "/thing/123",
            get(|| async { (StatusCode::NOT_FOUND, "Not found") }),
        )
        .route(
            "/policy",
            post(|| async {
                (
                    StatusCode::UNPROCESSABLE_ENTITY,
                    Json(json!({
                        "code": "POLICY_VIOLATION",
                        "reason": "test.rule",
                        "message": "Test violation",
                        "referenceError": "https://docs.bss-cli.dev/policies/test.rule",
                        "context": {"key": "value"},
                    })),
                )
            }),
        )
        .route(
            "/unprocessable",
            post(|| async {
                (
                    StatusCode::UNPROCESSABLE_ENTITY,
                    Json(json!({"detail": "validation error"})),
                )
            }),
        )
        .route(
            "/boom",
            get(|| async { (StatusCode::INTERNAL_SERVER_ERROR, "boom") }),
        )
        .route(
            "/down",
            get(|| async { (StatusCode::SERVICE_UNAVAILABLE, "down") }),
        )
        .route(
            "/forbidden",
            get(|| async { (StatusCode::FORBIDDEN, "nope") }),
        )
        .route("/ok", get(|| async { Json(json!({"ok": true})) }))
        .route(
            "/slow",
            get(|| async {
                tokio::time::sleep(Duration::from_millis(500)).await;
                "late"
            }),
        )
        .route("/echo-headers", get(echo_headers))
        .route("/flaky", get(flaky))
        .with_state(state);

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr: SocketAddr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    format!("http://{addr}")
}

async fn echo_headers(headers: HeaderMap) -> Json<Value> {
    let mut map = serde_json::Map::new();
    for (k, v) in &headers {
        map.insert(k.as_str().to_string(), json!(v.to_str().unwrap_or("")));
    }
    Json(Value::Object(map))
}

async fn flaky(State(s): State<AppState>) -> impl IntoResponse {
    s.flaky_calls.fetch_add(1, Ordering::SeqCst);
    (StatusCode::SERVICE_UNAVAILABLE, "down")
}

fn client(base: &str) -> BssClient {
    BssClient::new(base).unwrap()
}

// ─── typed errors ───────────────────────────────────────────────────────────

#[tokio::test]
async fn maps_404_to_not_found() {
    let base = spawn_peer().await;
    let err = client(&base)
        .request(Method::GET, "/thing/123", None, None)
        .await
        .unwrap_err();
    assert!(matches!(err, ClientError::NotFound(_)));
    assert_eq!(err.status_code(), 404);
}

#[tokio::test]
async fn maps_422_policy_violation() {
    let base = spawn_peer().await;
    let err = client(&base)
        .request(Method::POST, "/policy", None, None)
        .await
        .unwrap_err();
    match err {
        ClientError::Policy(pv) => {
            assert_eq!(pv.rule, "test.rule");
            assert_eq!(pv.context, json!({"key": "value"}));
        }
        other => panic!("expected Policy, got {other:?}"),
    }
}

#[tokio::test]
async fn maps_422_non_policy_to_http() {
    let base = spawn_peer().await;
    let err = client(&base)
        .request(Method::POST, "/unprocessable", None, None)
        .await
        .unwrap_err();
    assert!(matches!(err, ClientError::Http { status: 422, .. }));
    assert!(!matches!(err, ClientError::Policy(_)));
}

#[tokio::test]
async fn maps_5xx_to_server_error() {
    let base = spawn_peer().await;
    for (path, code) in [("/boom", 500u16), ("/down", 503)] {
        let err = client(&base)
            .request(Method::GET, path, None, None)
            .await
            .unwrap_err();
        assert!(matches!(err, ClientError::Server { .. }));
        assert_eq!(err.status_code(), code);
    }
}

#[tokio::test]
async fn maps_other_4xx_to_http() {
    let base = spawn_peer().await;
    let err = client(&base)
        .request(Method::GET, "/forbidden", None, None)
        .await
        .unwrap_err();
    assert!(matches!(err, ClientError::Http { status: 403, .. }));
}

#[tokio::test]
async fn success_returns_response() {
    let base = spawn_peer().await;
    let resp = client(&base)
        .request(Method::GET, "/ok", None, None)
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body, json!({"ok": true}));
}

#[tokio::test]
async fn per_call_timeout_raises() {
    let base = spawn_peer().await;
    let err = client(&base)
        .request(Method::GET, "/slow", None, Some(Duration::from_millis(50)))
        .await
        .unwrap_err();
    assert!(matches!(err, ClientError::Timeout(_)));
    assert_eq!(err.status_code(), 504);
}

#[tokio::test]
async fn no_automatic_retry_on_503() {
    // Build the app once so the flaky counter is shared across the single call.
    let state = AppState::default();
    let counter = state.flaky_calls.clone();
    let app = Router::new().route("/flaky", get(flaky)).with_state(state);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });

    let err = client(&format!("http://{addr}"))
        .request(Method::GET, "/flaky", None, None)
        .await
        .unwrap_err();
    assert!(matches!(err, ClientError::Server { status: 503, .. }));
    assert_eq!(counter.load(Ordering::SeqCst), 1, "must not retry");
}

// ─── auth + context propagation ─────────────────────────────────────────────

#[tokio::test]
async fn auth_and_context_headers_propagate() {
    let base = spawn_peer().await;
    let auth = Arc::new(TokenAuthProvider::new("tok-123").unwrap()) as Arc<dyn AuthProvider>;
    let c = BssClient::with_auth(&base, auth, Duration::from_secs(5)).unwrap();

    let ctx = RequestCtx {
        actor: "alice".to_string(),
        channel: "cli".to_string(),
        request_id: "req-42".to_string(),
        ..RequestCtx::default()
    };
    let resp = scope(ctx, c.request(Method::GET, "/echo-headers", None, None))
        .await
        .unwrap();
    let seen: Value = resp.json().await.unwrap();
    assert_eq!(seen["x-bss-api-token"], "tok-123");
    assert_eq!(seen["x-bss-actor"], "alice");
    assert_eq!(seen["x-bss-channel"], "cli");
    assert_eq!(seen["x-request-id"], "req-42");
}

#[tokio::test]
async fn default_context_generates_request_id() {
    let base = spawn_peer().await;
    // No scope → default ctx; request id is backfilled with a uuid.
    let resp = client(&base)
        .request(Method::GET, "/echo-headers", None, None)
        .await
        .unwrap();
    let seen: Value = resp.json().await.unwrap();
    assert_eq!(seen["x-bss-actor"], "system");
    assert!(!seen["x-request-id"].as_str().unwrap().is_empty());
}

// ─── auth providers ─────────────────────────────────────────────────────────

#[test]
fn auth_providers_shape_and_fail_fast() {
    assert!(NoAuthProvider.headers().is_empty());
    assert_eq!(
        TokenAuthProvider::new("t").unwrap().headers(),
        vec![("X-BSS-API-Token".to_string(), "t".to_string())]
    );
    assert!(TokenAuthProvider::new("").is_err());

    // NamedToken: primary env wins; fallback used when primary unset; else error.
    std::env::set_var("BSS_TEST_PRIMARY_API_TOKEN", "primary-tok");
    let p = NamedTokenAuthProvider::from_env(
        "portal_self_serve",
        "BSS_TEST_PRIMARY_API_TOKEN",
        Some("BSS_API_TOKEN"),
    )
    .unwrap();
    assert_eq!(p.identity(), "portal_self_serve");
    assert_eq!(p.source_env(), "BSS_TEST_PRIMARY_API_TOKEN");
    assert_eq!(p.headers()[0].1, "primary-tok");
    std::env::remove_var("BSS_TEST_PRIMARY_API_TOKEN");

    let err = NamedTokenAuthProvider::from_env("x", "BSS_DEFINITELY_UNSET_API_TOKEN", None);
    assert!(err.is_err());
}
