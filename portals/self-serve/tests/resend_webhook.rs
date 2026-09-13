#![allow(clippy::unwrap_used, clippy::expect_used)]

use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use base64::{engine::general_purpose::STANDARD, Engine};
use bss_self_serve::{build_router, build_state, AppState};
use hmac::{Hmac, Mac};
use http_body_util::BodyExt;
use sha2::Sha256;
use std::sync::Arc;
use tower::ServiceExt;

const KEY: &[u8] = b"local-resend-webhook-test-key";

fn state() -> AppState {
    let mut state = build_state();
    Arc::make_mut(&mut state.settings).email_resend_webhook_secret =
        format!("whsec_{}", STANDARD.encode(KEY));
    state.db = None;
    state
}

fn request(id: &str, payload: &str, signed_payload: &str) -> Request<Body> {
    let timestamp = chrono::Utc::now().timestamp().to_string();
    let mut mac = Hmac::<Sha256>::new_from_slice(KEY).unwrap();
    mac.update(format!("{id}.{timestamp}.{signed_payload}").as_bytes());
    let signature = STANDARD.encode(mac.finalize().into_bytes());
    Request::builder()
        .method("POST")
        .uri("/webhooks/resend")
        .header("svix-id", id)
        .header("svix-timestamp", timestamp)
        .header("svix-signature", format!("v1,{signature}"))
        .body(Body::from(payload.to_owned()))
        .unwrap()
}

#[tokio::test]
async fn resend_requires_signature_and_retries_without_storage() {
    let payload = r#"{"type":"email.delivered","data":{"email_id":"email-test"}}"#;
    let response = build_router(state())
        .oneshot(request("test", payload, "tampered"))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    let response = build_router(state())
        .oneshot(request("test", payload, payload))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    let mut unconfigured = state();
    Arc::make_mut(&mut unconfigured.settings)
        .email_resend_webhook_secret
        .clear();
    let response = build_router(unconfigured)
        .oneshot(request("test", payload, payload))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn resend_rejects_signed_invalid_payloads() {
    for payload in ["not-json", "{}", r#"{"type":""}"#] {
        let response = build_router(state())
            .oneshot(request("test", payload, payload))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    }
}

#[tokio::test]
#[ignore = "requires BSS_TEST_DB_URL pointing to the migrated local development database"]
async fn resend_persists_redacts_and_deduplicates() {
    let mut state = state();
    let pool = bss_db::connect(&std::env::var("BSS_TEST_DB_URL").unwrap())
        .await
        .unwrap();
    state.db = Some(pool.clone());
    let id = format!("setup-test-{}", uuid::Uuid::new_v4());
    let payload = r#"{"type":"email.delivered","data":{"email_id":"email-test","to":["test@example.invalid"],"from":"sender@example.invalid"}}"#;
    for duplicate in [false, true] {
        let response = build_router(state.clone())
            .oneshot(request(&id, payload, payload))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let bytes = response.into_body().collect().await.unwrap().to_bytes();
        let body: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(
            body.get("deduped")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            duplicate
        );
    }
    let stored: serde_json::Value = sqlx::query_scalar(
        "SELECT body FROM integrations.webhook_event WHERE provider='resend' AND event_id=$1",
    )
    .bind(&id)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(stored["data"]["to"], "[redacted]");
    assert_eq!(stored["data"]["from"], "[redacted]");
    assert_eq!(stored["data"]["email_id"], "email-test");
    sqlx::query("DELETE FROM integrations.webhook_event WHERE provider='resend' AND event_id=$1")
        .bind(&id)
        .execute(&pool)
        .await
        .unwrap();
}
